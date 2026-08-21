import logging
import re
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, ValidationError
from yaml import YAMLError, safe_dump, safe_load

from jri.lib import files, git, prompt
from jri.lib.context import estimate_tokens
from jri.lib.lock import Lock

from . import paths
from .exceptions import PersistenceError, RepositoryStateError, SpecsError
from .repository import ACCEPTANCE_TRAILER, Repository
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
    commit: str | None
    accepted: str | None
    notebook: bytes
    accepted_notebook: bytes
    # These are the specifications of the accepted commit. JRI reads them from the two roots that it writes.
    # If JRI read the whole tree, it would also read a file that the user, not JRI, put beside them.
    specifications: dict[str, bytes]


# Record an acceptance before it changes the project.
# An undo uses the patch, the commit of the last acceptance, and the indexed paths.
# This record says nothing about a lock. It can stay after the run ends, and it cannot identify an old holder.
class Acceptance(BaseModel):
    accepted: str | None
    patch: str
    indexed: tuple[str, ...]

    model_config = ConfigDict(extra="forbid")


class Specs:
    def __init__(self, path: Path) -> None:
        self.repository = Repository(path)
        self.workspace = Workspace(self.repository.path)

    def prepare(self) -> Baseline:
        notebook = self._read_notebook()
        self._reconcile()
        self._check_state()
        if not self.repository.has_commit():
            return Baseline(None, None, notebook, b"", {})
        commit = self.repository.read_head()
        specs = self.repository.read_tree(commit, paths.SPECS_DIR)
        accepted = self.repository.find_commit(ACCEPTANCE_TRAILER)
        if accepted is None:
            if specs:
                raise RepositoryStateError("Git holds specifications JRI did not write. Remove them before Ralphing.")
            return Baseline(commit, None, notebook, b"", {})
        specifications = self.repository.read_tree(accepted, paths.FUNCTIONAL_SPECS_DIR) | self.repository.read_tree(
            accepted, paths.ARCHITECTURE_SPECS_DIR
        )
        if specs != specifications:
            raise RepositoryStateError("Checked-out specifications differ from the ones JRI accepted.")
        logger.info("baseline_prepared head=%s accepted=%s specifications=%d", commit, accepted, len(specifications))
        return Baseline(
            commit, accepted, notebook, self.repository.read_file(accepted, paths.NOTEBOOK_FILE), specifications
        )

    # This states whether an earlier run left uncommitted specifications. The draft file alone records this state.
    # Git validates its content.
    @property
    def drafted(self) -> bool:
        return self.workspace.draft_file.exists()

    # A draft claims to apply to the current specifications. Git validates the whole patch before it writes any part.
    # A specification tree that JRI moved stays as the checkout left it.
    # Do not trust the Git exit status. Compare the trees before and after.
    # The draft is the delta, not the tree that Git checked out.
    # Validate the changed tree like model output, because a patch can create a link.
    # Drop an invalid draft, because it blocks every future run and forces the user to remove a JRI file.
    def resume(self, repository: git.Repository) -> tuple[str, ...] | None:
        draft = self._read_draft()
        standing = self._read_specification_tree(repository.path)
        status = repository.read_status()
        try:
            checked_out = self.read(repository, paths.SPECS_DIR)
            repository.apply_patch(draft, index=True)
            placed = self.read(repository, paths.SPECS_DIR)
            drafted = tuple(
                path for path in sorted(checked_out.keys() | placed.keys()) if checked_out.get(path) != placed.get(path)
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
                    "drafted. Nothing was committed. Your notes stand, and your project keeps the "
                    "specifications it already had."
                ) from error
        cls._stage(repository, [destination.relative_to(repository.path).as_posix() for destination in specifications])
        logger.info("specifications_written root=%s files=%d deleted=%d", model_root, len(written), len(deleted))

    # A specification must be a plain file. The filesystem and Git show links in different ways.
    # A Windows checkout can show a Git `120000` link as a normal file with target text.
    # Check Git links as well as filesystem links. A run reads this tree for the model and later commits it.
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

    # Save the work of this run for the next run, and return the patch that this run would commit.
    # Git creates a delta from the project specifications. Remove an empty draft, because it holds no new work.
    def save_draft(self, repository: git.Repository, baseline: Baseline) -> bytes:
        patch = repository.diff(baseline.commit, paths=(paths.FUNCTIONAL_SPECS_DIR, paths.ARCHITECTURE_SPECS_DIR))
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

    def accept(self, patch: bytes, baseline: Baseline) -> str:
        # A user commit can move HEAD without changing this run.
        # The specification tree that the patch used must remain unchanged.
        head_specs = (
            self.repository.read_tree(self.repository.read_head(), paths.SPECS_DIR)
            if self.repository.has_commit()
            else {}
        )
        if head_specs != baseline.specifications:
            raise RepositoryStateError("The specifications changed during generation. Try again.")
        if self._read_notebook() != baseline.notebook:
            raise RepositoryStateError("The project notes changed during generation. Try again.")
        self._check_state()
        # Record the acceptance before JRI changes the project.
        # An undo must not guess this state from the files that a run left.
        acceptance = Acceptance(
            accepted=baseline.accepted,
            patch=patch.decode(),
            indexed=self.repository.read_staged_paths(paths.COMMITTED_PATHS),
        )
        self.workspace.open_generation_dir()
        # Hold this lock only while the acceptance runs. The operating system releases it if the process ends.
        # A later run can find a live acceptance, and it does not have to trust a pid that the system gave again.
        with Lock(self.workspace.acceptance_lock_file):
            files.write_atomically(self.workspace.acceptance_file, acceptance.model_dump_json())
            try:
                self.repository.apply_patch(patch)
            except git.Error as error:
                # A disk, quota, or file-limit failure can stop Git during a specification write.
                # Undo the partial JRI patch before another run sees it. The log holds the Git error details.
                logger.exception("acceptance_write_failed characters=%d", len(patch))
                self._undo_acceptance(acceptance)
                raise SpecsError(
                    "JRI could not write the specifications into your project, so nothing was committed. Try again."
                ) from error
            try:
                # Stage intent only. JRI must not overwrite user-staged content at its paths.
                # Project ignore rules do not control this. JRI keeps `.jri` in Git even when the project ignores it.
                self.repository.stage(paths.COMMITTED_PATHS, intent_to_add=True, force=True)
                commit = self.repository.commit(
                    "jri: update specifications", trailers=(ACCEPTANCE_TRAILER,), paths=paths.COMMITTED_PATHS
                )
            except git.Error:
                commit = self._settle_acceptance(acceptance)
                if commit is None:
                    raise
            else:
                self._drop_acceptance()
        # The commit completes the draft work.
        # The project now holds these specifications. A run that resumed the delta would write them twice.
        self.workspace.drop_draft()
        logger.info("specs_committed commit=%s", commit)
        return commit

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

    # An acceptance that stopped can leave JRI specifications in the worktree without a commit.
    # Later runs refuse to start while those specifications are there.
    # The run offer stays active. Reconcile this state, so that the user does not have to delete JRI files.
    def _reconcile(self) -> None:
        if not self.workspace.acceptance_file.exists():
            return
        # If a process holds the record lock, an acceptance is active.
        # Its patch, index, and record belong to that run.
        # The operating system releases the lock at exit, but it can give a pid to another process.
        # Check the lock before you read the record. Do not settle a live acceptance when a temporary read fails.
        if Lock(self.workspace.acceptance_lock_file).is_held():
            return
        # JRI writes the index when it settles an acceptance.
        # Check the locks first, to avoid a Git error about a path inside `.git`.
        self._check_locks()
        acceptance = self._read_acceptance()
        if acceptance is None:
            self._settle_unreadable_acceptance()
            return
        self._settle_acceptance(acceptance)

    # The next Git command stops on these files and reports them, with a `.git` path during a specification run.
    # JRI only names them. A Git lock does not say who holds it, and the operating system does not release it.
    # An old JRI lock and a Git lock that a user command holds look the same on the disk.
    # The user must see their paths and then decide.
    def _check_locks(self) -> None:
        blocking = self.repository.locks.blocking
        if blocking:
            raise RepositoryStateError(
                "Git is locked. Wait for the command holding it, or, if none is running, remove these before "
                "Ralphing:\n" + "\n".join(f"- {path}" for path in blocking)
            )

    # An unreadable record does not state what its run applied, staged, or accepted before.
    # It still proves that an acceptance was in progress.
    # JRI settles a record that is cut, corrupt, or old. JRI does not remove such a record.
    def _read_acceptance(self) -> Acceptance | None:
        try:
            return Acceptance.model_validate_json(self.workspace.acceptance_file.read_bytes())
        except (OSError, ValidationError):
            logger.exception("acceptance_unreadable path=%s", self.workspace.acceptance_file)
            return None

    # JRI owns this record. If JRI cannot remove it, every later run reads it again.
    # Report this as a JRI failure.
    def _drop_acceptance(self) -> None:
        try:
            self.workspace.acceptance_file.unlink(missing_ok=True)
        except OSError as error:
            logger.exception("acceptance_removal_failed path=%r", self.workspace.acceptance_file)
            raise PersistenceError(
                f"Could not remove the acceptance record `{self.workspace.acceptance_file}`: {error.strerror}"
            ) from error

    # The Git exit status does not state what Git wrote.
    # A process can fail after the reference transaction creates a commit.
    # A kill of the whole run can return no status.
    # Find the commit with the trailer. Do not use the Git status.
    # Do not reverse a commit that exists, because that can delete user specifications.
    # Ask Git one time, because another command can fail first.
    def _settle_acceptance(self, acceptance: Acceptance) -> str | None:
        accepted = self.repository.find_commit(ACCEPTANCE_TRAILER)
        if accepted == acceptance.accepted:
            self._undo_acceptance(acceptance)
            return None
        # Git writes the commit from its own index before it copies that index to the project.
        # If Git fails between the two steps, it shows every committed specification as deleted.
        # After a commit, make the project index agree with it.
        if accepted is not None:
            self.repository.unstage(paths.COMMITTED_PATHS)
        self._drop_acceptance()
        logger.info("acceptance_committed commit=%s", accepted)
        return accepted

    # Settle an unreadable acceptance record.
    # JRI has no patch, no earlier index paths, and no earlier acceptance commit.
    # Do not undo worktree data or reset the paths that the user staged. Do not ask if an unknown commit exists.
    # A plain file that matches the bytes of the commit differs only in the index, whoever wrote it.
    # Restore its index entry, but change no data on the disk and no commit.
    # A filesystem link is not such a file.
    # A Git-only link is such an entry, because its link mode stays the same and the disk does not change.
    # Leave all other paths and the record for `_check_state` to report.
    # Remove the record only when all the specifications settle.
    def _settle_unreadable_acceptance(self) -> None:
        settled: list[str] = []
        # A first acceptance can fail in a project with no commit. No worktree content can then match a commit.
        if self.repository.has_commit():
            head = self.repository.read_head()
            for entry in self.repository.read_status(paths.COMMITTED_PATHS, ignored=True):
                standing = self.workspace.root / entry.path
                # Git refuses a path that the commit does not hold instead of returning bytes.
                with suppress(OSError, git.Error):
                    if (
                        not standing.is_symlink()
                        and standing.is_file()
                        and standing.read_bytes() == self.repository.read_file(head, entry.path)
                    ):
                        settled.append(entry.path)
        if settled:
            self.repository.unstage(settled)
        logger.info("acceptance_index_settled count=%d", len(settled))
        if not self.repository.read_status((paths.COMMITTED_SPECS,), ignored=True):
            self._drop_acceptance()

    def _undo_acceptance(self, acceptance: Acceptance) -> None:
        intended = self._rebuild_writes(acceptance)
        reversible: tuple[str, ...] | None = None
        if intended is not None:
            # Repair a partial write first. Git validates only the context lines of a hunk.
            # If JRI reverses a patch over a partial file, the reverse can succeed.
            # It then removes the content that remains.
            self._repair_writes(acceptance.accepted, intended)
            # Check the whole patch next.
            # A kill after Git applies the patch is normal, and Git validates the complete patch in one pass.
            reversible = (
                (acceptance.patch,)
                if self._can_apply(acceptance.patch, reverse=True)
                else self._plan_undo(acceptance.patch)
            )
        if reversible is None:
            # The path is neither JRI output, partial JRI output, nor its prior content.
            # JRI can also lack its intended content.
            # Do not remove the path. Keep the record and let `_check_state` name what the user must resolve.
            logger.info("acceptance_undo_refused accepted=%s", acceptance.accepted)
            return
        # Unstage only the entries that the acceptance staged.
        # If JRI unstages a path that the user staged, it can discard the content of that path.
        # Git restores `HEAD` content, or no content when `HEAD` has no path.
        added = [
            path for path in self.repository.read_staged_paths(paths.COMMITTED_PATHS) if path not in acceptance.indexed
        ]
        if added:
            self.repository.unstage(added)
        for file_patch in reversible:
            self.repository.apply_patch(file_patch.encode(), reverse=True)
        self._drop_acceptance()
        logger.info("acceptance_undone unstaged=%d reversed=%d", len(added), len(reversible))

    # A write that an acceptance did not complete holds no user data to keep.
    # Restore the tracked paths from their commit. Remove the partial writes that Git does not track.
    # Leave the links for `_check_state` to report.
    def _repair_writes(self, accepted: str | None, intended: dict[str, bytes]) -> None:
        tracked = self.repository.read_staged_paths((paths.COMMITTED_SPECS,))
        for path in (self.workspace.root / paths.SPECS_DIR).rglob("*.md"):
            relative = path.relative_to(self.workspace.root).as_posix()
            if relative not in tracked and path.is_file() and self._holds_part_of(path, intended.get(relative)):
                path.unlink()
                logger.info("part_written_spec_removed path=%s", relative)
        if accepted is None:
            return
        unwritten = [path for path in tracked if self._holds_part_of(self.workspace.root / path, intended.get(path))]
        if unwritten:
            self.repository.restore(accepted, unwritten)
            logger.info("part_written_specs_restored count=%d", len(unwritten))

    # Rebuild the intended writes from the recorded patch and the earlier commit.
    # This is the worktree that the acceptance would leave if nothing stopped it.
    # A rebuild that fails identifies no path. A rebuilt tree that JRI cannot read identifies no path either.
    # Do not undo when JRI can identify no path.
    def _rebuild_writes(self, acceptance: Acceptance) -> dict[str, bytes] | None:
        try:
            with self._open_pre_image(acceptance.accepted) as pre_image:
                pre_image.apply_patch(acceptance.patch.encode())
                return self.read(pre_image, paths.SPECS_DIR)
        except (OSError, git.Error, SpecsError):
            logger.exception("acceptance_rebuild_failed accepted=%s", acceptance.accepted)
            return None

    # Open the commit that the record names in its own worktree.
    # For a first acceptance, use an empty repository.
    @contextmanager
    def _open_pre_image(self, accepted: str | None) -> Generator[git.Repository]:
        location = self.workspace.root / paths.PRE_IMAGE_DIR
        if accepted is not None:
            with self.repository.open_worktree(accepted, location=location) as worktree:
                yield worktree
            return
        files.remove_directory(location)
        # Refuse a location that stays after the removal above.
        # An empty repository over old specifications would rebuild writes that no acceptance made.
        location.mkdir(parents=True)
        try:
            yield git.Repository.init(location, nested=True)
        finally:
            files.remove_directory(location)

    # A partial write leaves no file, or it leaves the first part of the target.
    # `git apply` removes a file before it makes the file again.
    # Neither state is the intended specification or the earlier one.
    # If the rebuilt tree has no such path, the acceptance meant to remove it.
    # Do not restore data at a path that the acceptance meant to remove.
    @staticmethod
    def _holds_part_of(path: Path, intended: bytes | None) -> bool:
        if intended is None or path.is_symlink():
            return False
        content = path.read_bytes() if path.is_file() else b""
        return content != intended and intended.startswith(content)

    # `git apply` validates the full patch and then writes the files one by one.
    # A kill can leave any first part of the patch on the disk.
    # Check each file patch. Git wrote a patch that JRI can reverse. Git never reached a patch that JRI can apply.
    # A patch that is neither one can hold user edits.
    # Do not undo any file when such a path needs the user to look at it.
    def _plan_undo(self, patch: str) -> tuple[str, ...] | None:
        reversible: list[str] = []
        for file_patch in self._split_patch(patch):
            if self._can_apply(file_patch, reverse=True):
                reversible.append(file_patch)
            elif not self._can_apply(file_patch, reverse=False):
                return None
        return tuple(reversible)

    def _can_apply(self, patch: str, *, reverse: bool) -> bool:
        try:
            self.repository.apply_patch(patch.encode(), check=True, reverse=reverse)
        except git.Error:
            return False
        return True

    @staticmethod
    def _split_patch(patch: str) -> list[str]:
        lines = patch.splitlines(keepends=True)
        # Only the patch metadata identifies a changed file. Every line in the body of a hunk has a prefix.
        # A header in column zero is a file header.
        bounds = [*(number for number, line in enumerate(lines) if line.startswith("diff --git ")), len(lines)]
        return ["".join(lines[start:end]) for start, end in pairwise(bounds)]

    # A draft can reach a commit without model output from this run.
    # It stays on the disk of the user, and a newer JRI can read it.
    # Newer rules can refuse a name that an older draft allowed.
    # Validate its changes as `Specs.write` validates model output.
    # Check every added entry, not only Markdown. A patch can add files that no later round reads or commit names.
    # Compare with the checkout. Names, case folds, and files that already exist are not changes of this draft.
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
    # Compare the restored worktree with the checkout.
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
                "JRI could not take a drafted specification back out of the worktree it was writing in, so nothing "
                "was committed. Your project keeps the specifications it already had. Try again."
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
    # A restore removes only the files that appeared after the checkout.
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
    # Stage every path that JRI touched before it reads the acceptance diff.
    # `git add` rejects a command with a missing path.
    # Stage the files that stay on the disk and the paths that JRI removed from Git.
    # Force the staging, because JRI keeps `.jri` in Git even when the project ignore rules exclude it.
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

    def _check_state(self) -> None:
        self._check_locks()
        # Outside a branch, Git makes a commit that only a detached `HEAD` can reach.
        # A return to the branch loses that commit.
        # A stopped rebase, a bisection, and a checkout of a commit or a tag have this state.
        # Rebase refs cannot identify this state each time.
        if not self.repository.is_on_branch():
            raise RepositoryStateError(
                "Git is not on a branch, so JRI's commit would be lost. Check out a branch before Ralphing."
            )
        # A merge and cherry-pick keep the branch, but Git refuses a partial commit during either.
        # `MERGE_HEAD` marks even a clean merge. A cherry-pick stops only at its conflict.
        if self.repository.has_conflicts() or self.repository.has_commit("MERGE_HEAD"):
            raise RepositoryStateError("Finish the merge or cherry-pick in progress before Ralphing.")
        # JRI stages the ignored files too, so this check must use the same scope.
        # A user file below a JRI path still belongs to the user.
        # A check that does not read the ignore rules could commit that file.
        blockers = sorted(entry.path for entry in self.repository.read_status((paths.COMMITTED_SPECS,), ignored=True))
        if blockers:
            raise RepositoryStateError(
                "Commit or remove these files before Ralphing:\n" + "\n".join(f"- {path}" for path in blockers)
            )
        # The files that JRI commits must meet the same link rules as the model paths.
        # Git stores a link as target text.
        # A notebook or a specification that is a link can show a model a file that JRI must keep from it.
        # Check both the filesystem and Git.
        # Each one can report a link that the other cannot, above all a Windows `120000` entry.
        # Use filesystem paths for the filesystem check, and Git paths for the Git check.
        committed = (
            self.workspace.settings_file,
            self.workspace.gitignore_file,
            self.workspace.notebook_file,
            *(self.workspace.root / paths.SPECS_DIR).rglob("*.md"),
        )
        links = sorted(
            {path.relative_to(self.workspace.root).as_posix() for path in committed if path.is_symlink()}
            | set(self.repository.read_staged_paths(paths.COMMITTED_PATHS, linked=True))
        )
        if links:
            raise RepositoryStateError(
                "JRI writes plain files, and these are links. Replace them before Ralphing:\n"
                + "\n".join(f"- {path}" for path in links)
            )

    # A case-insensitive filesystem reads two variants of the same name as one file.
    # The committed tree must work on every platform, and Windows and macOS cannot keep both names.
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
