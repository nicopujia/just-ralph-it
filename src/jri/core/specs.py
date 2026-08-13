import logging
import re
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from pydantic import BaseModel, ConfigDict, ValidationError
from yaml import YAMLError, safe_dump, safe_load

from jri.lib import files, git, prompt
from jri.lib.lock import Lock

from . import paths
from .exceptions import PersistenceError, RepositoryStateError, SpecsError
from .repository import Repository
from .workspace import Workspace

# This trailer identifies the commit that accepted a generation. Git can then find that commit.
ACCEPTANCE_TRAILER = "JRI-Specifications: accepted"
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
# A written file carries its summary as YAML frontmatter, so the index can never drift from the file it describes.
FRONTMATTER = re.compile(r"\A---\n(?P<meta>.*?)\n---\n\n?", re.DOTALL)

logger = logging.getLogger(__name__)


# What a model reads or writes: a specification's path, its full body, and a one-line summary for the index.
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
    functional: dict[str, bytes]
    architecture: dict[str, bytes]


# Record an acceptance before it changes the project. Undo uses the patch, prior acceptance commit, and indexed paths.
# This record says nothing about a lock. It can outlive the run and cannot identify a past lock holder.
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
            return Baseline(None, None, notebook, b"", {}, {})
        commit = self.repository.read_head()
        specs = self.repository.read_tree(commit, paths.SPECS_DIR)
        accepted = self.repository.find_commit(ACCEPTANCE_TRAILER)
        if accepted is None:
            if specs:
                raise RepositoryStateError("Git holds specifications JRI did not write. Remove them before Ralphing.")
            return Baseline(commit, None, notebook, b"", {}, {})
        functional = self.repository.read_tree(accepted, paths.FUNCTIONAL_SPECS_DIR)
        architecture = self.repository.read_tree(accepted, paths.ARCHITECTURE_SPECS_DIR)
        if specs != functional | architecture:
            raise RepositoryStateError("Checked-out specifications differ from the ones JRI accepted.")
        logger.info("baseline_prepared head=%s accepted=%s functional=%d", commit, accepted, len(functional))
        return Baseline(
            commit,
            accepted,
            notebook,
            self.repository.read_file(accepted, paths.NOTEBOOK_FILE),
            functional,
            architecture,
        )

    # This states whether an earlier run left uncommitted specifications. The draft file alone records this state.
    # Git validates its content.
    @property
    def drafted(self) -> bool:
        return self.workspace.draft_file.exists()

    # A draft claims to apply to the current specifications. Git validates the whole patch before it writes any part.
    # A moved specification tree remains as the checkout left it.
    # Do not trust Git exit status; compare trees before and after.
    # The draft is the delta, not the checked-out tree.
    # Validate the changed tree like model output because a patch can create a link.
    # Drop an invalid draft before it blocks every future run and forces the user to remove a JRI file.
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

    def write(
        self, repository: git.Repository, files: Mapping[str, str], deleted: Sequence[str], model_root: str
    ) -> None:
        if not files and not deleted:
            raise SpecsError("Specifications must change at least one file.")
        # A null character makes Git treat a file as binary. A binary diff has no content, and `git apply` rejects it.
        # Otherwise, JRI would blame its write for model text.
        binary = next((path for path, content in sorted(files.items()) if "\x00" in content), None)
        if binary is not None:
            raise SpecsError(f"Specifications are text, and `{binary}` holds a null character.")
        root = repository.path / paths.SPECS_DIR
        # A path in both lists is written and removed by the model. The removal takes precedence.
        specifications: dict[Path, str | None] = {
            self._locate_specification(repository.path, path, model_root): content for path, content in files.items()
        } | {self._locate_specification(repository.path, path, model_root): None for path in deleted}
        folded = self._find_folded_names(root, model_root, (*files, *deleted))
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
            # A file-system refusal can identify a model path: an invalid name or a directory where a file belongs.
            # End the run with that path instead of reporting a JRI fault.
            except (OSError, ValueError) as error:
                logger.exception("specification_write_failed path=%s", destination)
                raise SpecsError(
                    f"JRI could not write the specification `{destination.relative_to(root).as_posix()}` it "
                    "drafted. Nothing was committed. Your notes stand, and your project keeps the "
                    "specifications it already had."
                ) from error
        self._stage(repository, [destination.relative_to(repository.path).as_posix() for destination in specifications])
        logger.info("specifications_written root=%s files=%d deleted=%d", model_root, len(files), len(deleted))

    # A specification must be a plain file. The file system and Git represent links differently.
    # A Windows checkout can show a Git `120000` link as a normal file with target text.
    # Check Git links as well as file-system links. A run reads this tree for the model and later commits it.
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
            # A link is not a specification to Git or the file system.
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

    # Full content, frontmatter stripped, for files a model chose to read in full.
    @staticmethod
    def render(files: dict[str, bytes]) -> str:
        rendered: list[str] = []
        for name, _, body in Specs._decode_all(files):
            # The model names the file and writes its body. Quote the name for the same reason as the body.
            # An unquoted name with a line break can create a second `file` block inside JRI text.
            rendered.append(prompt.render(file=name, content=body))
        return "\n\n".join(rendered) or "(empty)"

    # Path and one-line summary only, for every file — cheap enough to always include in full.
    @staticmethod
    def index(files: dict[str, bytes]) -> str:
        entries = {name: summary or "(no summary)" for name, summary, _ in Specs._decode_all(files)}
        return prompt.render(specifications=entries) if entries else "(empty)"

    # Frontmatter carries the summary a model gave a file when it wrote it.
    @staticmethod
    def format(file: File) -> str:
        frontmatter = safe_dump({"summary": file.summary}, sort_keys=False, allow_unicode=True, width=10**9)
        return f"---\n{frontmatter}---\n\n{file.content}"

    # Save the current run work for the next run and return the patch that this run would commit.
    # Git creates a delta from the project specifications. Remove an empty draft because it carries no new work.
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
        # The draft preserves work.
        # A failed draft write would lose that work and block every later run for the same reason.
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
        if head_specs != baseline.functional | baseline.architecture:
            raise RepositoryStateError("The specifications changed during generation. Try again.")
        if self._read_notebook() != baseline.notebook:
            raise RepositoryStateError("The project notes changed during generation. Try again.")
        self._check_state()
        # Record acceptance before changing the project. Undo must not infer this state from files left by a run.
        acceptance = Acceptance(
            accepted=baseline.accepted,
            patch=patch.decode(),
            indexed=self.repository.read_staged_paths(paths.COMMITTED_PATHS),
        )
        self.workspace.open_generation_dir()
        # Hold this lock only while acceptance runs. The operating system drops it if the process is killed.
        # A later run can detect a live acceptance without trusting a reused PID.
        with Lock(self.workspace.acceptance_lock_file):
            files.write_atomically(self.workspace.acceptance_file, acceptance.model_dump_json())
            try:
                self.repository.apply_patch(patch)
            except git.Error as error:
                # A disk, quota, or file-limit failure can stop Git during a specification write.
                # Undo the partial JRI patch before another run sees it. Git error details are in the log.
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
        # The project now holds these specifications, so resuming the delta would write them twice.
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
            # JRI writes UTF-8 here. Non-UTF-8 bytes from Git were not written by JRI.
            # Deciding their model text belongs to the user.
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

    # A killed acceptance can leave JRI specifications in the worktree without a commit.
    # Later runs refuse to start over them.
    # The run offer remains active. Reconcile this state so the user need not delete JRI files.
    def _reconcile(self) -> None:
        if not self.workspace.acceptance_file.exists():
            return
        # A held record lock means that acceptance is active. Its patch, index, and record belong to that run.
        # The operating system releases the lock at exit, unlike a PID that it can reuse.
        # Check the lock before reading the record. Do not settle a live acceptance when a temporary read fails.
        if Lock(self.workspace.acceptance_lock_file).is_held():
            return
        # Settlement writes the index. Check locks first to avoid a Git error about a path inside `.git`.
        self._check_locks()
        acceptance = self._read_acceptance()
        if acceptance is None:
            self._settle_unreadable_acceptance()
            return
        self._settle_acceptance(acceptance)

    # The next Git command reports and stops on these files, with a `.git` path during a specification run.
    # JRI only names them. A Git lock has no owner mark and the operating system does not release it.
    # A stale JRI lock and an active user Git lock have the same disk shape.
    # The user must decide after seeing their paths.
    def _check_locks(self) -> None:
        blocking = self.repository.locks.blocking
        if blocking:
            raise RepositoryStateError(
                "Git is locked. Wait for the command holding it, or, if none is running, remove these before "
                "Ralphing:\n" + "\n".join(f"- {path}" for path in blocking)
            )

    # An unreadable record does not state what its run applied, staged, or previously accepted.
    # It still proves that acceptance was in progress. A truncated, corrupt, or older record is settled, not removed.
    def _read_acceptance(self) -> Acceptance | None:
        try:
            return Acceptance.model_validate_json(self.workspace.acceptance_file.read_bytes())
        except (OSError, ValidationError):
            logger.exception("acceptance_unreadable path=%s", self.workspace.acceptance_file)
            return None

    # This record is JRI-owned. A failed removal makes every later run read it again, so report this as a JRI failure.
    def _drop_acceptance(self) -> None:
        try:
            self.workspace.acceptance_file.unlink(missing_ok=True)
        except OSError as error:
            logger.exception("acceptance_removal_failed path=%r", self.workspace.acceptance_file)
            raise PersistenceError(
                f"Could not remove the acceptance record `{self.workspace.acceptance_file}`: {error.strerror}"
            ) from error

    # Git exit status does not state what Git wrote.
    # A process can fail after the reference transaction creates a commit.
    # A full-run kill can return no status. Find the commit with the trailer instead of using Git status.
    # Do not reverse an existing commit because it can delete user specifications.
    # Ask Git once; another command can fail first.
    def _settle_acceptance(self, acceptance: Acceptance) -> str | None:
        accepted = self.repository.find_commit(ACCEPTANCE_TRAILER)
        if accepted == acceptance.accepted:
            self._undo_acceptance(acceptance)
            return None
        # Git writes the commit from its own index before copying that index to the project.
        # A failure between steps leaves every committed specification shown as deleted.
        # After a commit, make the project index match it.
        if accepted is not None:
            self.repository.unstage(paths.COMMITTED_PATHS)
        self._drop_acceptance()
        logger.info("acceptance_committed commit=%s", accepted)
        return accepted

    # Settle an unreadable acceptance record without its patch, prior index paths, or prior acceptance commit.
    # Do not undo worktree data or reset user-staged paths. Do not ask whether an unknown commit exists.
    # A plain file that matches commit bytes differs only in the index, regardless of its author.
    # Restore its index entry without changing disk data or any commit. A file-system link is not such a file.
    # A Git-only link is such an entry because restoring its existing link mode changes nothing on disk.
    # Leave all other paths and the record for `_check_state` to report.
    # Remove the record only when all specifications settle.
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
            # Repair a partial write first. Git validates only hunk context lines.
            # Reversing a patch over a partial file can succeed and remove its remaining content.
            self._repair_writes(acceptance.accepted, intended)
            # Check the whole patch next.
            # A kill after application is normal, and Git validates the complete patch in one pass.
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
        # Unstage only entries that acceptance staged. Unstaging a user-staged path can discard its content.
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

    # A partial acceptance write contains no user data to preserve. Restore tracked paths from their commit.
    # Remove untracked partial writes. Leave links for `_check_state` to report.
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

    # Rebuild intended writes from the recorded patch and prior commit.
    # This is the worktree that acceptance would leave without interruption.
    # A failed rebuild does not identify any path. An unreadable rebuilt tree does not identify one either.
    # Do not undo when no path can be identified.
    def _rebuild_writes(self, acceptance: Acceptance) -> dict[str, bytes] | None:
        try:
            with self._open_pre_image(acceptance.accepted) as pre_image:
                pre_image.apply_patch(acceptance.patch.encode())
                return self.read(pre_image, paths.SPECS_DIR)
        except (git.Error, SpecsError):
            logger.exception("acceptance_rebuild_failed accepted=%s", acceptance.accepted)
            return None

    # Open the commit named by the record in its own worktree. For a first acceptance, use an empty repository.
    @contextmanager
    def _open_pre_image(self, accepted: str | None) -> Generator[git.Repository]:
        if accepted is not None:
            with self.repository.open_worktree(accepted, parent=self.workspace.open_worktree_dir()) as worktree:
                yield worktree
            return
        with TemporaryDirectory(prefix="jri-rebuild-", dir=self.workspace.open_worktree_dir()) as directory:
            yield git.Repository.init(directory, nested=True)

    # A partial write leaves either no file or an initial part of the target.
    # `git apply` removes a file before recreating it.
    # Neither state is the intended or prior specification. A missing rebuilt path was meant for deletion.
    # Do not restore data at a path that acceptance meant to remove.
    @staticmethod
    def _holds_part_of(path: Path, intended: bytes | None) -> bool:
        if intended is None or path.is_symlink():
            return False
        content = path.read_bytes() if path.is_file() else b""
        return content != intended and intended.startswith(content)

    # `git apply` validates the full patch and then writes files one by one. A kill can leave any patch prefix on disk.
    # Check each file patch. A reversible patch was written; an applicable patch was never reached.
    # A patch that is neither can have user edits. Do not undo any file when such a path requires user review.
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
        # Only patch metadata identifies a changed file. Every hunk body line has a prefix.
        # A column-zero header is therefore a file header.
        bounds = [*(number for number, line in enumerate(lines) if line.startswith("diff --git ")), len(lines)]
        return ["".join(lines[start:end]) for start, end in pairwise(bounds)]

    # A draft can reach a commit without current model output.
    # It persists on the user disk and can be read by newer JRI.
    # Newer rules can reject a name that an older draft allowed.
    # Validate its changes as `Specs.write` validates model output.
    # Check every added entry, not only Markdown. A patch can add files that no later round reads or commit names.
    # Compare against the checkout. Existing names, case folds, and files are not changes that this draft must validate.
    @classmethod
    def _check_specifications(
        cls, worktree: Path, standing: Mapping[str, bytes | None], placed: Mapping[str, bytes | None]
    ) -> None:
        prefix = f"{paths.SPECS_DIR}/"
        added = {path.removeprefix(prefix) for path in placed.keys() - standing.keys()}
        for path, content in sorted(placed.items()):
            name = path.removeprefix(prefix)
            # A root is JRI text for a model and a draft claim. The name must state its root before validation.
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

    # Remove everything that a refused draft placed. Git reverses what it can; an unapplied draft has nothing to remove.
    # Restore specifications from the bytes that JRI read. Do not trust Git status.
    # `git apply --reverse` can exit successfully after reversing only a repeated path section.
    # Compare the restored worktree with the checkout. Do not let another round write to an unaccounted worktree.
    # This temporary worktree belongs to this run and is removed when it ends.
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
        # A partial restore leaves an uncertain worktree. Read it below to determine its state.
        except (OSError, git.Error):
            logger.exception("draft_restore_failed worktree=%s", repository.path)
        if self._read_specification_tree(repository.path) != standing or repository.read_status() != status:
            raise SpecsError(
                "JRI could not take a drafted specification back out of the worktree it was writing in, so nothing "
                "was committed. Your project keeps the specifications it already had. Try again."
            )

    # Remove entries absent from `standing` and rewrite entries with different bytes.
    # Return each changed path for the index.
    # Do not overwrite an entry with no `standing` bytes because JRI never read it.
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

    # Read every specification-tree entry.
    # Store bytes that JRI can restore and `None` for links, sockets, and unreadable files.
    # `Specs.read` defines a specification. This method records what exists so restore removes only checkout additions.
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

    # `git diff` ignores untracked files. Stage every path touched by JRI before reading the acceptance diff.
    # `git add` rejects a command with a missing path. Stage files left on disk and paths removed from Git.
    # Force staging because JRI keeps `.jri` in Git despite project ignore rules.
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
        # Outside a branch, Git creates a commit reachable only from detached `HEAD`. Returning to the branch loses it.
        # Stopped rebases, bisections, and commit or tag checkouts have this state.
        # Rebase refs cannot identify it reliably.
        if not self.repository.is_on_branch():
            raise RepositoryStateError(
                "Git is not on a branch, so JRI's commit would be lost. Check out a branch before Ralphing."
            )
        # A merge and cherry-pick keep the branch, but Git refuses a partial commit during either.
        # `MERGE_HEAD` marks even a clean merge. A cherry-pick stops only at its conflict.
        if self.repository.has_conflicts() or self.repository.has_commit("MERGE_HEAD"):
            raise RepositoryStateError("Finish the merge or cherry-pick in progress before Ralphing.")
        # Staging reaches ignored files, so this check must use the same scope.
        # A user file under a JRI path remains user-owned.
        # A check that ignores ignore rules could commit that file.
        blockers = sorted(entry.path for entry in self.repository.read_status((paths.COMMITTED_SPECS,), ignored=True))
        if blockers:
            raise RepositoryStateError(
                "Commit or remove these files before Ralphing:\n" + "\n".join(f"- {path}" for path in blockers)
            )
        # JRI committed files must meet the same link rules as model paths. Git stores a link as target text.
        # A linked notebook or specification can expose a file that JRI must not show a model.
        # Check both the file system and Git.
        # Each can report a link that the other cannot, especially Windows `120000` entries.
        # Use file-system paths for the file-system check and Git paths for the Git check.
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

    # Case-insensitive file systems treat two case variants as one file. The committed tree must work on every platform.
    # Windows and macOS cannot retain both names. A case-only rename writes and then removes the same file.
    # Compare written names with existing `*.md` names. A `Path` gives the same case fold on a case-insensitive system.
    @staticmethod
    def _find_folded_names(root: Path, model_root: str, written: Iterable[str]) -> tuple[str, str] | None:
        standing = {path.relative_to(root).as_posix() for path in (root / model_root).rglob("*.md")}
        found: dict[str, str] = {}
        for name in sorted(standing | {PurePosixPath(path).as_posix() for path in written}):
            first = found.setdefault(name.lower(), name)
            if first != name:
                return first, name
        return None

    # Locate a model path only as a Markdown file in its own root. It must meet `_names_a_file` rules and avoid links.
    # Validate every model-written file here.
    @classmethod
    def _locate_specification(cls, worktree: Path, raw_path: str, model_root: str) -> Path:
        path = PurePosixPath(raw_path)
        destination = worktree / paths.SPECS_DIR / path
        try:
            # A link between the worktree and file bypasses the path rules below.
            # Resolve from the Git worktree to detect this link.
            # This also detects a link where a JRI directory belongs. An unresolvable name is not a specification.
            located = destination.resolve().parent.is_relative_to(worktree.resolve() / paths.SPECS_DIR / model_root)
        except (OSError, ValueError):
            located = False
        if not cls._names_a_file(path) or path.suffix != ".md" or not path.is_relative_to(model_root) or not located:
            raise SpecsError(f"Specifications cannot change `{raw_path}`.")
        return destination

    # Every path part must avoid traversal, file-system roots, and a directory that a specification glob matches.
    # `Specs.read` reads every `*.md` match. Case-insensitive systems can treat `notes.MD` as the `notes.md` directory.
    # Each name must work on all platforms and must not be a Git pathspec pattern.
    @staticmethod
    def _names_a_file(path: PurePosixPath) -> bool:
        if path.is_absolute() or ".." in path.parts or any(part.lower().endswith(".md") for part in path.parts[:-1]):
            return False
        return bool(path.parts) and all(
            SPECIFICATION_NAME.fullmatch(part) is not None
            and part.partition(".")[0].upper() not in WINDOWS_DEVICE_NAMES
            for part in path.parts
        )
