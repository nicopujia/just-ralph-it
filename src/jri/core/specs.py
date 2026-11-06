import logging
import re
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict
from yaml import YAMLError, safe_dump, safe_load

from jri.lib import files, git, prompt
from jri.lib.context import estimate_tokens

from . import paths
from .exceptions import PersistenceError, RepositoryStateError, SpecsError
from .repository import Repository
from .workspace import Workspace

# Specification names use this allowed ASCII set. Each name is both a file name and a Git pathspec.
# Windows rejects control characters and `<>:"/\|?*`, and removes trailing spaces and dots.
# Git reads `*?[]\` in a pathspec as patterns. JRI-owned English roots do not need non-ASCII names.
# The project language belongs in file content, not these names.
SPECIFICATION_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._-]*[A-Za-z0-9_-])?")
# Windows resolves these names to devices regardless of case or extension. No file can use one as its name.
WINDOWS_DEVICE_NAMES = frozenset({
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{port}" for port in "123456789"),
    *(f"LPT{port}" for port in "123456789"),
})
# A file that JRI writes holds its summary as YAML frontmatter.
# The index always agrees with the file that it describes.
FRONTMATTER = re.compile(r"\A---\n(?P<meta>.*?)\n---\n\n?", re.DOTALL)

logger = logging.getLogger(__name__)


# A model reads and writes the path of a specification, its full body, and a one-line summary for the index.
class File(BaseModel):
    path: str
    content: str
    summary: str

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class Baseline:
    notebook: bytes
    # This is the notebook of the last run that wrote specifications. A project with no such run has none.
    accepted_notebook: bytes | None
    # These are the specifications on the disk. JRI reads them from the two roots that it writes.
    # If JRI read the whole tree, it would also read a file that the user, not JRI, put beside them.
    specifications: dict[str, bytes]


class Specs:
    def __init__(self, path: Path) -> None:
        self.repository = Repository(path)
        self.workspace = Workspace(self.repository.path)

    def prepare(self) -> Baseline:
        notebook = self._read_notebook()
        self._check_links()
        specifications = self._read_specifications(self.repository)
        logger.info("baseline_prepared specifications=%d", len(specifications))
        return Baseline(notebook, self._read_accepted_notebook(), specifications)

    # A run writes in a private repository that holds only a copy of the project specifications.
    # The project history and index stay out of the run. The caller reads the base tree from this copy.
    @contextmanager
    def open_staging(self, baseline: Baseline) -> Generator[Repository]:
        location = self.workspace.reserve_worktree_dir()
        files.remove_directory(location)
        # Refuse a location that stays after the removal above. An old index there would give a wrong base tree.
        location.mkdir()
        try:
            repository = Repository.init(location, nested=True)
            repository.isolate()
            self._stage(repository, self._write_specification_tree(location, baseline.specifications))
            yield repository
        finally:
            files.remove_directory(location)

    # This states whether an earlier run left drafted specifications. The draft file alone records this state.
    # Git validates its content.
    @property
    def drafted(self) -> bool:
        return self.workspace.draft_file.exists()

    # A draft claims to apply to the current specifications. Git validates the whole patch before it writes any part.
    # A specification tree that JRI moved stays as the copy left it.
    # Do not trust the Git exit status. Compare the trees before and after.
    # The draft is the delta, not the tree that Git checked out.
    # Validate the changed tree like model output, because a patch can create a link.
    # Drop an invalid draft, because it blocks every future run and forces the user to remove a JRI file.
    def resume(self, repository: git.Repository) -> tuple[str, ...] | None:
        draft = self._read_draft()
        standing = self._read_specification_tree(repository.path)
        status = repository.read_status()
        try:
            copied = self.read(repository, paths.SPECS_DIR)
            repository.apply_patch(draft, index=True)
            placed = self.read(repository, paths.SPECS_DIR)
            drafted = tuple(
                path for path in sorted(copied.keys() | placed.keys()) if copied.get(path) != placed.get(path)
            )
            self._check_specifications(repository.path, standing, self._read_specification_tree(repository.path))
        except (git.Error, SpecsError) as error:
            logger.info("draft_refused characters=%d reason=%s", len(draft), error)
        else:
            if drafted:
                return drafted
            logger.info("draft_placed_nothing characters=%d", len(draft))
        self.workspace.drop_draft()
        self._restore_specifications(repository, draft, standing, status)
        return None

    # A model writes as many times as one pass needs, so a write can arrive with one file or with twenty.
    @classmethod
    def write(
        cls, repository: git.Repository, written: Mapping[str, str], deleted: Sequence[str], model_root: str
    ) -> None:
        if not written and not deleted:
            raise SpecsError("Specifications must change at least one file.")
        # A null character makes Git treat a file as binary. A binary diff has no content, and `git apply` rejects it.
        # Without this check, JRI would report a fault in its own write, but the model text made the failure.
        binary = next((path for path, content in sorted(written.items()) if "\x00" in content), None)
        if binary is not None:
            raise SpecsError(f"Specifications are text, and `{binary}` holds a null character.")
        # A file with a summary and no body is a placeholder for work that no later pass does.
        # Refuse the file here, in the write. The model then gets the refusal while it can still write the file.
        empty = next(
            (path for path, content in sorted(written.items()) if not cls._split_frontmatter(content)[1].strip()), None
        )
        if empty is not None:
            raise SpecsError(f"Specifications carry the behavior they name, and `{empty}` carries none.")
        root = repository.path / paths.SPECS_DIR
        # The model can write a path and also remove it. JRI then removes that path.
        specifications: dict[Path, str | None] = {
            cls._locate_specification(repository.path, path, model_root): content for path, content in written.items()
        } | {cls._locate_specification(repository.path, path, model_root): None for path in deleted}
        folded = cls._find_folded_names(root, model_root, (*written, *deleted))
        if folded is not None:
            raise SpecsError(
                f"Specifications cannot hold both `{folded[0]}` and `{folded[1]}`, which some filesystems read as "
                "one file."
            )
        for destination, content in specifications.items():
            try:
                # Remove the target path instead of opening it.
                # JRI then overwrites a link instead of writing through it.
                destination.unlink(missing_ok=True)
                if content is not None:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(content, encoding="utf-8", newline="")
            # A filesystem can refuse a model path.
            # The name can be invalid, or a directory can be where a file belongs.
            # End the run with that path. Do not report a fault in JRI.
            except (OSError, ValueError) as error:
                logger.exception("specification_write_failed path=%s", destination)
                raise SpecsError(
                    f"JRI could not write the specification `{destination.relative_to(root).as_posix()}` it "
                    "drafted. Your notes stand, and your project keeps the specifications it already had."
                ) from error
        cls._stage(repository, [destination.relative_to(repository.path).as_posix() for destination in specifications])
        logger.info("specifications_written root=%s files=%d deleted=%d", model_root, len(written), len(deleted))

    # A specification must be a plain file. The filesystem and Git show links in different ways.
    # A Windows checkout can show a Git `120000` link as a normal file with target text.
    # Check Git links as well as filesystem links. A run reads this tree for the model and later applies it.
    # `selected` names files the same way `render`/`index` show them: relative to `paths.SPECS_DIR`, root included.
    # Omit it to read every specification under `directory`.
    @staticmethod
    def read(repository: git.Repository, directory: str, selected: Iterable[str] | None = None) -> dict[str, bytes]:
        allowed = frozenset(selected) if selected is not None else None
        linked = frozenset(repository.read_staged_paths((directory,), linked=True))
        specifications: dict[str, bytes] = {}
        for path in sorted((repository.path / directory).rglob("*.md")):
            relative = path.relative_to(repository.path).as_posix()
            if allowed is not None and relative.removeprefix(f"{paths.SPECS_DIR}/") not in allowed:
                continue
            # A link is not a specification to Git or the filesystem.
            # Directories, pipes, and sockets are not specifications either.
            # Report the path inside the tree, not a temporary worktree path that the user did not request.
            if relative in linked or path.is_symlink() or not path.is_file():
                raise SpecsError(f"JRI writes plain specification files, and `{relative}` is not one.")
            try:
                specifications[relative] = path.read_bytes()
            except OSError as error:
                logger.exception("specification_read_failed path=%s", relative)
                raise SpecsError(f"JRI could not read the specification `{relative}`: {error.strerror}") from error
        return specifications

    # Read the files that a model named under one root. Refuse the read when a name matches no file.
    # A model reads what it never wrote, so the names it gives are a request, not JRI data.
    # Raise the failure that the tool loop reports to that model.
    # Do not raise a `SpecsError` about the specifications themselves.
    # `cap` is the token limit of one answer. A specification that JRI cut reads like a complete one.
    # The model would then design from the part that arrived.
    # Refuse the batch, and give the cost of each file.
    # A call that names one file answers with that file at any size, because no smaller request for it exists.
    @staticmethod
    def read_selected(repository: git.Repository, model_root: str, selected: Sequence[str], cap: int) -> str:
        prefix = f"{paths.SPECS_DIR}/"
        found = Specs.read(repository, f"{prefix}{model_root}", selected=selected)
        missing = sorted(set(selected) - {path.removeprefix(prefix) for path in found})
        if missing:
            raise RuntimeError(f"Could not find these {model_root} specifications: {', '.join(missing)}.")
        weights = {path.removeprefix(prefix): estimate_tokens(len(content)) for path, content in sorted(found.items())}
        total = sum(weights.values())
        if len(found) > 1 and total > cap:
            raise RuntimeError(
                f"These {model_root} specifications weigh {total} tokens together, over the {cap} tokens one call "
                f"answers with: {', '.join(f'{name} ({weight})' for name, weight in weights.items())}. "
                "Ask for fewer paths."
            )
        return Specs.render(found)

    # This gives the full content of the files that a model chose to read, without the frontmatter.
    @staticmethod
    def render(files: dict[str, bytes]) -> str:
        rendered: list[str] = []
        for name, _, body in Specs._decode_all(files):
            # The model names the file and writes its body. Quote the name for the same reason as the body.
            # An unquoted name with a line break can create a second `file` block inside JRI text.
            rendered.append(prompt.render(file=name, content=body))
        return "\n\n".join(rendered) or "(empty)"

    # This gives only the path and the one-line summary of every file.
    # It is small, so JRI always includes all of it.
    @staticmethod
    def index(files: dict[str, bytes]) -> str:
        entries = {name: summary or "(no summary)" for name, summary, _ in Specs._decode_all(files)}
        return prompt.render(specifications=entries) if entries else "(empty)"

    # Frontmatter holds the summary that a model gave a file when it wrote it.
    @staticmethod
    def format(file: File) -> str:
        frontmatter = safe_dump({"summary": file.summary}, sort_keys=False, allow_unicode=True, width=10**9)
        return f"---\n{frontmatter}---\n\n{file.content}"

    # Save the work of this run for the next run, and return the patch that this run would apply.
    # Git creates a delta from the base tree. Remove an empty draft, because it holds no new work.
    def save_draft(self, repository: git.Repository, base: str) -> bytes:
        patch = repository.diff(base, paths=(paths.FUNCTIONAL_SPECS_DIR, paths.ARCHITECTURE_SPECS_DIR))
        if not patch:
            self.workspace.drop_draft()
            return patch
        # The JRI ignore file excludes this directory.
        # The draft stays out of `git add -A`, repository copies, and architect input.
        self.workspace.open_generation_dir()
        try:
            files.write_atomically(self.workspace.draft_file, patch.decode())
            logger.info("draft_saved characters=%d", len(patch))
        # The draft keeps the work.
        # If JRI could not write the draft, it would lose that work and block every later run for the same reason.
        except OSError:
            logger.exception("draft_write_failed path=%r", self.workspace.draft_file)
        return patch

    # Write the specifications of the private repository into the project. JRI makes no commit.
    # The user decides what enters the history, so Git cannot restore a specification that this write loses.
    # The notebook record goes last. A stop before that write leaves the notes as changes for the next run.
    def accept(self, repository: git.Repository, baseline: Baseline) -> None:
        if self._read_specifications(self.repository) != baseline.specifications:
            raise RepositoryStateError("The specifications changed during generation. Try again.")
        if self._read_notebook() != baseline.notebook:
            raise RepositoryStateError("The project notes changed during generation. Try again.")
        self._check_links()
        wanted = {**self._read_specifications(repository), paths.ACCEPTED_NOTEBOOK_FILE: baseline.notebook}
        standing = dict(baseline.specifications)
        if baseline.accepted_notebook is not None:
            standing[paths.ACCEPTED_NOTEBOOK_FILE] = baseline.accepted_notebook
        changed = {path: content for path, content in wanted.items() if standing.get(path) != content}
        self._check_targets(changed)
        try:
            self._replace_files(changed, sorted(standing.keys() - wanted.keys()), standing)
        except OSError as error:
            logger.exception("acceptance_write_failed files=%d", len(changed))
            raise SpecsError(
                f"JRI could not write the specifications into your project: {error.strerror}. Try again."
            ) from error
        # The project now holds these specifications. A run that resumed the delta would write them twice.
        self.workspace.drop_draft()
        logger.info("specs_accepted files=%d", len(changed))

    # `rglob` does not enter a link to a directory, so `_check_links` does not find one.
    # A write through that link would replace a file that JRI never read.
    def _check_targets(self, changed: Mapping[str, bytes]) -> None:
        prefix = f"{paths.SPECS_DIR}/"
        for path in changed:
            if path.startswith(prefix):
                name = path.removeprefix(prefix)
                self._locate_specification(self.workspace.root, name, PurePosixPath(name).parts[0])

    # Write every new file beside its target first. A full disk or a read-only directory then changes no target.
    # Remove files before the moves. On a case-insensitive filesystem, a rename can name one file with two paths.
    # Each move replaces a file whole, so a stop leaves the earlier version or the new one of each file.
    def _replace_files(
        self, changed: Mapping[str, bytes], removed: Sequence[str], standing: Mapping[str, bytes]
    ) -> None:
        temporary: dict[str, Path] = {}
        touched: list[str] = []
        try:
            for path, content in changed.items():
                temporary[path] = files.write_beside(self.workspace.root / path, content)
            self._move_files(temporary, removed, touched)
        except OSError:
            self._put_back(touched, standing)
            raise
        finally:
            for file in temporary.values():
                file.unlink(missing_ok=True)

    # Record each path as it changes. A failure then names what to put back.
    def _move_files(self, temporary: Mapping[str, Path], removed: Sequence[str], touched: list[str]) -> None:
        for path in removed:
            (self.workspace.root / path).unlink()
            touched.append(path)
        for path, file in temporary.items():
            file.replace(self.workspace.root / path)
            touched.append(path)

    # Git holds no copy of uncommitted files, so put back each one that JRI can.
    # A file that JRI cannot put back must not stop the files after it.
    # Remove new files first. On a case-insensitive filesystem, a rename can name one file with two paths.
    def _put_back(self, touched: Iterable[str], standing: Mapping[str, bytes]) -> None:
        for path in sorted(touched, key=lambda path: path in standing):
            try:
                self._restore_file(self.workspace.root / path, standing.get(path))
            except OSError:
                logger.exception("specification_restore_failed path=%s", path)

    @staticmethod
    def _restore_file(target: Path, content: bytes | None) -> None:
        if content is None:
            target.unlink()
            return
        replacement = files.write_beside(target, content)
        try:
            replacement.replace(target)
        finally:
            replacement.unlink(missing_ok=True)

    @staticmethod
    def _decode_all(files: dict[str, bytes]) -> list[tuple[str, str, str]]:
        prefix = f"{paths.SPECS_DIR}/"
        decoded: list[tuple[str, str, str]] = []
        for path, content in sorted(files.items()):
            name = path.removeprefix(prefix)
            try:
                body = content.decode()
            # JRI writes UTF-8, so bytes that are not UTF-8 came from somewhere else.
            # Only the user can decide what text a model reads from them. JRI refuses them.
            except UnicodeDecodeError as error:
                raise SpecsError(f"Specifications are UTF-8 text, and `{name}` is not.") from error
            summary, body = Specs._split_frontmatter(body)
            decoded.append((name, summary, body))
        return decoded

    @staticmethod
    def _split_frontmatter(body: str) -> tuple[str, str]:
        match = FRONTMATTER.match(body)
        if not match:
            return "", body
        try:
            meta = safe_load(match["meta"])
        # A file JRI did not write, or a corrupted one, has no readable frontmatter. Treat its whole body as content.
        except YAMLError:
            return "", body
        return meta.get("summary", "") if isinstance(meta, dict) else "", body[match.end() :]

    # A draft can reach the project without model output from this run.
    # It stays on the disk of the user, and a newer JRI can read it.
    # Newer rules can refuse a name that an older draft allowed.
    # Validate its changes as `Specs.write` validates model output.
    # Check every added entry, not only Markdown. A patch can add files that no later round reads.
    # Compare with the copy. Names, case folds, and files that already exist are not changes of this draft.
    @classmethod
    def _check_specifications(
        cls, worktree: Path, standing: Mapping[str, bytes | None], placed: Mapping[str, bytes | None]
    ) -> None:
        prefix = f"{paths.SPECS_DIR}/"
        added = {path.removeprefix(prefix) for path in placed.keys() - standing.keys()}
        for path, content in sorted(placed.items()):
            name = path.removeprefix(prefix)
            # A root is JRI text for a model and a draft claim.
            # The name must state its root before JRI validates it.
            if name in added:
                model_root = PurePosixPath(name).parts[0]
                if model_root not in paths.SPECS_ROOTS:
                    raise SpecsError(f"Specifications cannot change `{name}`.")
                cls._locate_specification(worktree, name, model_root)
            if content is not None and content != standing.get(path) and b"\x00" in content:
                raise SpecsError(f"Specifications are text, and `{name}` holds a null character.")
        for model_root in paths.SPECS_ROOTS:
            folded = cls._find_folded_names(worktree / paths.SPECS_DIR, model_root, ())
            if folded is not None and added & set(folded):
                raise SpecsError(
                    f"Specifications cannot hold both `{folded[0]}` and `{folded[1]}`, which some filesystems read "
                    "as one file."
                )

    # Remove everything that a refused draft placed. Git reverses what it can.
    # A draft that Git did not apply has nothing to remove.
    # Restore the specifications from the bytes that JRI read. Do not trust the Git status.
    # `git apply --reverse` can exit with success after it reverses only one section of a repeated path.
    # Compare the restored worktree with the copy.
    # Another round must not write in a worktree whose state JRI does not know.
    # This temporary worktree belongs to this run, and the run removes it when it ends.
    def _restore_specifications(
        self,
        repository: git.Repository,
        draft: bytes,
        standing: Mapping[str, bytes | None],
        status: Sequence[git.Status],
    ) -> None:
        with suppress(git.Error):
            repository.apply_patch(draft, index=True, reverse=True)
        try:
            self._stage(repository, self._write_specification_tree(repository.path, standing))
        # A partial restore leaves a worktree that JRI does not know. Read it below to find its state.
        except (OSError, git.Error):
            logger.exception("draft_restore_failed worktree=%s", repository.path)
        if self._read_specification_tree(repository.path) != standing or repository.read_status() != status:
            raise SpecsError(
                "JRI could not take a drafted specification back out of the worktree it was writing in. "
                "Your project keeps the specifications it already had. Try again."
            )

    # Remove the entries that `standing` does not hold, and write the entries with different bytes again.
    # Return each changed path for the index.
    # Do not overwrite an entry that has no `standing` bytes, because JRI never read it.
    @classmethod
    def _write_specification_tree(cls, worktree: Path, standing: Mapping[str, bytes | None]) -> list[str]:
        remaining = cls._read_specification_tree(worktree)
        touched = sorted(remaining.keys() - standing.keys())
        for relative in touched:
            (worktree / relative).unlink()
        for relative, content in sorted(standing.items()):
            if content is None or remaining.get(relative) == content:
                continue
            destination = worktree / relative
            destination.unlink(missing_ok=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            touched.append(relative)
        return touched

    # Read every entry of the specification tree.
    # Store the bytes that JRI can restore, and `None` for links, sockets, and files that JRI cannot read.
    # `Specs.read` defines a specification. This method records what exists.
    # A restore removes only the files that appeared after the copy.
    @staticmethod
    def _read_specification_tree(worktree: Path) -> dict[str, bytes | None]:
        tree: dict[str, bytes | None] = {}
        for path in sorted((worktree / paths.SPECS_DIR).rglob("*")):
            if path.is_dir() and not path.is_symlink():
                continue
            content: bytes | None = None
            if path.is_file() and not path.is_symlink():
                try:
                    content = path.read_bytes()
                except OSError:
                    logger.exception("specification_entry_unreadable path=%s", path)
            tree[path.relative_to(worktree).as_posix()] = content
        return tree

    # `git diff` ignores the files that Git does not track.
    # Stage every path that JRI touched before it reads the diff.
    # `git add` rejects a command with a missing path.
    # Stage the files that stay on the disk and the paths that JRI removed from Git.
    # Force the staging, because the global ignore rules of the user can exclude a specification.
    @staticmethod
    def _stage(repository: git.Repository, touched: Sequence[str]) -> None:
        if not touched:
            return
        staged = sorted(
            {path for path in touched if (repository.path / path).is_file()}
            | set(repository.read_staged_paths(touched))
        )
        if staged:
            repository.stage(staged, force=True)

    # An unreadable or empty draft returns an empty patch. Git refuses that patch like any draft that does not apply.
    # Tell the run that the draft no longer fits.
    def _read_draft(self) -> bytes:
        try:
            return self.workspace.draft_file.read_bytes()
        except OSError:
            logger.exception("draft_read_failed path=%r", self.workspace.draft_file)
            return b""

    def _read_notebook(self) -> bytes:
        try:
            return self.workspace.notebook_file.read_bytes()
        except OSError as error:
            logger.exception("notebook_read_failed path=%r", self.workspace.notebook_file)
            raise PersistenceError(
                f"Could not read the notebook file `{self.workspace.notebook_file}`: {error.strerror}"
            ) from error

    # A missing record means that no run wrote specifications here. An unreadable record shows no changes.
    def _read_accepted_notebook(self) -> bytes | None:
        try:
            return self.workspace.accepted_notebook_file.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            logger.exception("accepted_notebook_read_failed path=%r", self.workspace.accepted_notebook_file)
            return None

    def _read_specifications(self, repository: git.Repository) -> dict[str, bytes]:
        return self.read(repository, paths.FUNCTIONAL_SPECS_DIR) | self.read(repository, paths.ARCHITECTURE_SPECS_DIR)

    # The files that JRI reads and writes obey the link rules of the model paths.
    # Git stores a link as target text.
    # A notebook or a specification that is a link can show a model a file that JRI must keep from it.
    # Check both the filesystem and Git.
    # Each one can report a link that the other cannot, above all a Windows `120000` entry.
    # Use filesystem paths for the filesystem check, and Git paths for the Git check.
    # JRI does not change the index. A reset can leave an entry for a link that the disk no longer holds.
    def _check_links(self) -> None:
        plain = (
            self.workspace.settings_file,
            self.workspace.gitignore_file,
            self.workspace.notebook_file,
            self.workspace.accepted_notebook_file,
            *(self.workspace.root / paths.SPECS_DIR).rglob("*.md"),
        )
        links = sorted(
            {path.relative_to(self.workspace.root).as_posix() for path in plain if path.is_symlink()}
            | {
                path
                for path in self.repository.read_staged_paths(paths.PLAIN_PATHS, linked=True)
                if (self.workspace.root / path).exists()
            }
        )
        if links:
            raise RepositoryStateError(
                "JRI writes plain files, and these are links. Replace them and stage them before Ralphing:\n"
                + "\n".join(f"- {path}" for path in links)
            )

    # A case-insensitive filesystem reads two variants of the same name as one file.
    # The specification tree must work on every platform, and Windows and macOS cannot keep both names.
    # A rename that changes only the case writes the file and then removes it.
    # Compare the new names with the `*.md` names that exist.
    # A `Path` gives the same case fold on a case-insensitive system.
    @staticmethod
    def _find_folded_names(root: Path, model_root: str, written: Iterable[str]) -> tuple[str, str] | None:
        standing = {path.relative_to(root).as_posix() for path in (root / model_root).rglob("*.md")}
        found: dict[str, str] = {}
        for name in sorted(standing | {PurePosixPath(path).as_posix() for path in written}):
            first = found.setdefault(name.lower(), name)
            if first != name:
                return first, name
        return None

    # Locate a model path only as a Markdown file in its own root.
    # It must meet the `_names_a_file` rules, and it must not be a link.
    # Validate here every file that a model writes.
    @classmethod
    def _locate_specification(cls, worktree: Path, raw_path: str, model_root: str) -> Path:
        path = PurePosixPath(raw_path)
        destination = worktree / paths.SPECS_DIR / path
        try:
            # A link between the worktree and file bypasses the path rules below.
            # Resolve from the Git worktree to detect this link.
            # This also finds a link where a JRI directory belongs.
            # A name that JRI cannot resolve is not a specification.
            located = destination.resolve().parent.is_relative_to(worktree.resolve() / paths.SPECS_DIR / model_root)
        except (OSError, ValueError):
            located = False
        if not cls._names_a_file(path) or path.suffix != ".md" or not path.is_relative_to(model_root) or not located:
            raise SpecsError(f"Specifications cannot change `{raw_path}`.")
        return destination

    # A part of a path must not move up the tree or name a filesystem root.
    # It also must not name a directory that a specification glob matches.
    # `Specs.read` reads every `*.md` match.
    # A case-insensitive system can read `notes.MD` as the `notes.md` directory.
    # Each name must work on all platforms, and it must not be a Git pathspec pattern.
    @staticmethod
    def _names_a_file(path: PurePosixPath) -> bool:
        if path.is_absolute() or ".." in path.parts or any(part.lower().endswith(".md") for part in path.parts[:-1]):
            return False
        return bool(path.parts) and all(
            SPECIFICATION_NAME.fullmatch(part) is not None
            and part.partition(".")[0].upper() not in WINDOWS_DEVICE_NAMES
            for part in path.parts
        )
