import logging
import os
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from pydantic import BaseModel, ConfigDict, ValidationError

from jri.lib import files, git, prompt
from jri.lib.lock import Lock

from . import paths
from .exceptions import PersistenceError, RepositoryStateError, SpecsError
from .repository import Repository
from .workspace import Workspace

# What the commit that accepted a generation calls itself, so Git can
# answer which commit that was.
ACCEPTANCE_TRAILER = "JRI-Specifications: accepted"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Baseline:
    commit: str | None
    accepted: str | None
    notebook: bytes
    accepted_notebook: bytes
    functional: dict[str, bytes]
    architecture: dict[str, bytes]


# An acceptance under way, written down before it touches the project
# so that undoing it never depends on reading the worktree back: the
# patch is what was applied, `accepted` is the acceptance commit the
# project held before it, `indexed` the paths Git already tracked, and
# `pid` the run carrying it out, so a lock left in `.git` beside this
# is told from one the run reading it is holding right now.
class Acceptance(BaseModel):
    accepted: str | None
    patch: str
    indexed: tuple[str, ...]
    pid: int

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

    def apply(self, repository: git.Repository, patch: str, model_root: str) -> None:
        self._validate_patch(patch, model_root)
        try:
            # A model writes hunks with no trailing context, which Git
            # otherwise takes for a patch against the end of a file and
            # refuses anywhere else. The lines a hunk quotes still have
            # to be in the file, `_validate_patch` above still allows no
            # path but a Markdown specification under the model's own
            # root, and the worktree this lands in is one the run throws
            # away -- so what a hunk gains is the freedom to sit
            # elsewhere in a file JRI wrote and is about to diff.
            repository.apply_patch(patch.encode(), index=True, directory=paths.SPECS_DIR, zero_context=True)
        except git.Error:
            # The patch is the only evidence of why generation failed.
            logger.exception("patch_rejected root=%s patch=%r", model_root, patch)
            raise
        logger.info("patch_applied root=%s characters=%d", model_root, len(patch))

    @staticmethod
    def read(worktree: Path, directory: str) -> dict[str, bytes]:
        root = worktree / directory
        return {path.relative_to(worktree).as_posix(): path.read_bytes() for path in sorted(root.rglob("*.md"))}

    @staticmethod
    def render(files: dict[str, bytes]) -> str:
        prefix = f"{paths.SPECS_DIR}/"
        # The path is JRI's own: an rglob over the specification tree
        # named it, under a root `_validate_patch` bounds. So it stays
        # prose, while the body a model wrote is quoted.
        return (
            "\n\n".join(
                f"File: {path.removeprefix(prefix)}\n{prompt.render(content=content.decode())}"
                for path, content in sorted(files.items())
            )
            or "(empty)"
        )

    def accept(self, patch: bytes, baseline: Baseline) -> str:
        # A commit the user makes mid-run moves HEAD without touching
        # what this run is about, so what has to have held still is the
        # specification tree the patch was written against.
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
        # Written down before the project is touched, so that undoing
        # this acceptance never has to be worked out from what a run
        # left behind -- least of all by a run that is not the one that
        # left it.
        acceptance = Acceptance(
            accepted=baseline.accepted,
            patch=patch.decode(),
            indexed=self.repository.read_staged_paths(paths.COMMITTED_PATHS),
            pid=os.getpid(),
        )
        self.workspace.open_generation_dir()
        # Taken for exactly the span in which the Git commands below can
        # leave a lock behind in `.git`, and dropped by the operating
        # system when a kill ends that span, so the run that reads the
        # record back learns whether the run that wrote it is still
        # there without asking a pid the system may have handed on.
        with Lock(self.workspace.acceptance_lock_file):
            files.write_atomically(self.workspace.acceptance_file, acceptance.model_dump_json())
            try:
                self.repository.apply_patch(patch)
            except git.Error as error:
                # A write the kernel cuts off -- a full disk, a quota, a
                # file limit -- dies inside Git with part of a
                # specification where a whole one was going, which is
                # this run's to take back rather than the next run's to
                # meet. Git's own words about it are in the log: what
                # they are about is a patch of JRI's the user never saw.
                logger.exception("acceptance_write_failed characters=%d", len(patch))
                self._undo_acceptance(acceptance)
                raise SpecsError(
                    "JRI could not write the specifications into your project, so nothing was committed. Try again."
                ) from error
            try:
                # The intent alone, so JRI never writes over content the
                # user staged for a path of its own. What the project
                # ignores does not decide this: `.jri` is JRI's to keep
                # in Git, and a project that ignores it Ralphs like any
                # other rather than failing once the generation has run.
                self.repository.stage(paths.COMMITTED_PATHS, intent_to_add=True, force=True)
                commit = self.repository.commit(
                    "jri: update specifications", trailers=(ACCEPTANCE_TRAILER,), paths=paths.COMMITTED_PATHS
                )
            except git.Error:
                self._undo_acceptance(acceptance)
                raise
            self._drop_acceptance()
        logger.info("specs_committed commit=%s", commit)
        return commit

    # An acceptance the operating system killed halfway leaves JRI's
    # own specifications in the worktree with no commit holding them,
    # and every later run refuses to start over them. The offer that
    # starts a run stands through that refusal, so without this the
    # only way out is for the user to delete files JRI wrote.
    def _reconcile(self) -> None:
        acceptance = self._read_acceptance()
        if acceptance is None:
            return
        self._release_index_lock(acceptance)
        # Undoing writes the index, so a lock still standing here would
        # come back as Git's own words about a path inside `.git`.
        self._check_index_lock()
        accepted = self.repository.find_commit(ACCEPTANCE_TRAILER) if self.repository.has_commit() else None
        # The kill may have landed after Git wrote the commit, and past
        # that point the patch is the project's: reversing it would
        # delete specifications the user has.
        if accepted != acceptance.accepted:
            self._drop_acceptance()
            logger.info("acceptance_committed commit=%s", accepted)
            return
        self._undo_acceptance(acceptance)

    # Git's own guard against two commands writing the index at once,
    # so JRI takes it away only where it can say whose it is: a record
    # says an acceptance was under way, a pid that is not this one says
    # the run doing it was another, and a lock the operating system has
    # already dropped says that run is dead. Anything else is a lock
    # JRI cannot account for, and `_check_index_lock` names it instead.
    def _release_index_lock(self, acceptance: Acceptance) -> None:
        index_lock_file = self.repository.index_lock_file
        if not index_lock_file.exists():
            return
        if acceptance.pid == os.getpid() or Lock(self.workspace.acceptance_lock_file).is_held():
            return
        index_lock_file.unlink(missing_ok=True)
        logger.info("index_lock_released path=%s", index_lock_file)

    # Whichever Git command meets this file next says so and stops,
    # which is a message about a path inside `.git` in the middle of a
    # run about specifications. What a killed acceptance of JRI's left
    # is already gone by here, so what stands is either a command
    # running now or one nothing of JRI's accounts for -- and either
    # way the user is the one who can tell which, once they are told
    # which file it is.
    def _check_index_lock(self) -> None:
        if self.repository.index_lock_file.exists():
            raise RepositoryStateError(
                "Git's index is locked. Wait for the command holding it, or, if none is running, remove "
                f"`{self.repository.index_lock_file}` before Ralphing."
            )

    # A record JRI cannot read says nothing about the run that wrote
    # it: not what that run applied, not what it staged, not whether
    # it is still there. So it stops being evidence and goes, rather
    # than standing in front of every run after it -- and whatever the
    # run it described left in the worktree, `_check_state` names.
    def _read_acceptance(self) -> Acceptance | None:
        if not self.workspace.acceptance_file.exists():
            return None
        try:
            return Acceptance.model_validate_json(self.workspace.acceptance_file.read_bytes())
        except (OSError, ValidationError):
            logger.exception("acceptance_unreadable path=%s", self.workspace.acceptance_file)
            self._drop_acceptance()
            return None

    # The record is JRI's own file, and a run that cannot take it away
    # leaves every run after it reading the same one, so what it says
    # about that is JRI's own rather than the operating system's.
    def _drop_acceptance(self) -> None:
        try:
            self.workspace.acceptance_file.unlink(missing_ok=True)
        except OSError as error:
            logger.exception("acceptance_removal_failed path=%r", self.workspace.acceptance_file)
            raise PersistenceError(
                f"Could not remove the acceptance record `{self.workspace.acceptance_file}`: {error.strerror}"
            ) from error

    def _undo_acceptance(self, acceptance: Acceptance) -> None:
        intended = self._rebuild_writes(acceptance)
        reversible: tuple[str, ...] | None = None
        if intended is not None:
            # What a cut-off write left goes back first, because Git
            # weighs a patch by the lines its hunks quote and nothing
            # else: a file the write stopped part way through still
            # holds those lines, so reversing the patch over it
            # succeeds and leaves the rest of the file gone for good.
            self._repair_writes(acceptance.accepted, intended)
            # The whole patch next: a kill that lands past the
            # application is the ordinary one, and Git weighs the lot
            # in a single pass.
            reversible = (
                (acceptance.patch,)
                if self._can_apply(acceptance.patch, reverse=True)
                else self._plan_undo(acceptance.patch)
            )
        if reversible is None:
            # What is there is neither what JRI wrote, nor a beginning
            # of it, nor what stood before it -- or JRI cannot say what
            # it was writing there at all. Either way it is not JRI's
            # to remove: the record stays, and whatever the user has to
            # sort out `_check_state` names.
            logger.info("acceptance_undo_refused accepted=%s", acceptance.accepted)
            return
        # Only the entries the acceptance staged come back out.
        # Resetting a path the user had staged themselves would throw
        # their content away instead, since Git puts back whatever HEAD
        # holds for it, and nothing at all when HEAD does not hold it
        # yet.
        added = [
            path for path in self.repository.read_staged_paths(paths.COMMITTED_PATHS) if path not in acceptance.indexed
        ]
        if added:
            self.repository.unstage(added)
        for file_patch in reversible:
            self.repository.apply_patch(file_patch.encode(), reverse=True)
        self._drop_acceptance()
        logger.info("acceptance_undone unstaged=%d reversed=%d", len(added), len(reversible))

    # What a write of the acceptance's was cut off part way through
    # holds nothing for anyone to lose, so what Git tracks comes back
    # from the commit that holds it, and what Git never tracked, being
    # this write's own, goes. The links `_check_state` refuses are left
    # for it to name.
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

    # What the acceptance was writing where, worked out rather than
    # recorded: the patch is in the record and what stood before it is
    # in the commit the record names, so applying the one to the other
    # is the worktree the acceptance would have left had nothing cut it
    # off. A rebuild Git cannot carry out says nothing about any path,
    # and an undo with nothing to say leaves every leftover standing.
    def _rebuild_writes(self, acceptance: Acceptance) -> dict[str, bytes] | None:
        try:
            with self._open_pre_image(acceptance.accepted) as pre_image:
                pre_image.apply_patch(acceptance.patch.encode())
                return self.read(pre_image.path, paths.SPECS_DIR)
        except git.Error:
            logger.exception("acceptance_rebuild_failed accepted=%s", acceptance.accepted)
            return None

    # Where the acceptance was writing: the commit the record names,
    # checked out on its own, or a repository holding nothing at all
    # where a first acceptance found no specification of JRI's.
    @contextmanager
    def _open_pre_image(self, accepted: str | None) -> Generator[git.Repository]:
        if accepted is not None:
            with self.repository.open_worktree(accepted) as worktree:
                yield worktree
            return
        with TemporaryDirectory(prefix="jri-rebuild-") as directory:
            yield git.Repository.init(directory)

    # What a write cut off leaves where a specification was going: the
    # file gone, since `git apply` removes one before making it again,
    # or a beginning of what was going there, which is where a bound
    # the kernel puts on the write stops it. Neither is the
    # specification the acceptance was writing and neither is the one
    # that stood before it. A path the rebuild holds nothing for is one
    # the acceptance meant gone -- a deletion, the far side of a rename
    # -- so what stands there is nobody's to put back.
    @staticmethod
    def _holds_part_of(path: Path, intended: bytes | None) -> bool:
        if intended is None or path.is_symlink():
            return False
        content = path.read_bytes() if path.is_file() else b""
        return content != intended and intended.startswith(content)

    # `git apply` validates a whole patch and only then writes it, file
    # by file, so a kill inside it leaves an arbitrary prefix of the
    # patch on disk -- the state reversing the whole recorded patch
    # refuses, and the state an acceptance is killed in. So each file
    # is weighed on its own: one Git can reverse is one the acceptance
    # wrote, one Git can still apply is one the kill never reached, and
    # one that is neither the user has since edited. That last one
    # takes the whole undo with it, because a file JRI cannot put back
    # is a file the user has to decide about, and deciding means seeing
    # it beside the rest of what the run left.
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
        # Only the metadata of a patch says what it changes, and every
        # line of a hunk body carries a prefix, so a header at column
        # zero is the header it reads as.
        bounds = [*(number for number, line in enumerate(lines) if line.startswith("diff --git ")), len(lines)]
        return ["".join(lines[start:end]) for start, end in pairwise(bounds)]

    def _read_notebook(self) -> bytes:
        try:
            return self.workspace.notebook_file.read_bytes()
        except OSError as error:
            logger.exception("notebook_read_failed path=%r", self.workspace.notebook_file)
            raise PersistenceError(
                f"Could not read the notebook file `{self.workspace.notebook_file}`: {error.strerror}"
            ) from error

    def _check_state(self) -> None:
        self._check_index_lock()
        # Off a branch, Git takes the commit and leaves it reachable
        # only from where HEAD stands, so going back to the branch
        # loses it: every stopped rebase, every bisect, and every
        # checkout of a commit or a tag sits here. The refs a rebase
        # writes cannot stand in for this, since one of them outlives
        # the rebase and a rebase held at a break writes none at all.
        if not self.repository.is_on_branch():
            raise RepositoryStateError(
                "Git is not on a branch, so JRI's commit would be lost. Check out a branch before Ralphing."
            )
        # A merge and a cherry-pick both keep the branch, and Git
        # refuses a partial commit under either. A merge says so with
        # MERGE_HEAD even when it merged cleanly, and a cherry-pick
        # only ever stops with the conflict that stopped it.
        if self.repository.has_conflicts() or self.repository.has_commit("MERGE_HEAD"):
            raise RepositoryStateError("Finish the merge or cherry-pick in progress before Ralphing.")
        # The staging reaches past whatever the project ignores, so
        # this reaches exactly as far, and over exactly what that
        # staging takes: a file of the user's under a path of JRI's
        # stays theirs however Git was told to treat it, and a check
        # blind to those rules would let the commit sweep it up.
        blockers = sorted(entry.path for entry in self.repository.read_status((paths.COMMITTED_SPECS,), ignored=True))
        if blockers:
            raise RepositoryStateError(
                "Commit or remove these files before Ralphing:\n" + "\n".join(f"- {path}" for path in blockers)
            )
        # What `_validate_patch` refuses inside a patch, the files JRI
        # commits may not be either. Git records a link as the text of
        # its target, so the notebook comes back out of the commit as a
        # path where the notes should be, and a specification read out
        # of a worktree is whatever the link points at -- a file that
        # was never JRI's to show a model. These are `COMMITTED_PATHS`
        # again, as the filesystem rather than Git spells them.
        committed = (
            self.workspace.config_file,
            self.workspace.gitignore_file,
            self.workspace.notebook_file,
            *(self.workspace.root / paths.SPECS_DIR).rglob("*.md"),
        )
        links = sorted(path.relative_to(self.workspace.root).as_posix() for path in committed if path.is_symlink())
        if links:
            raise RepositoryStateError(
                "JRI writes plain files, and these are links. Replace them before Ralphing:\n"
                + "\n".join(f"- {path}" for path in links)
            )

    @staticmethod
    def _validate_patch(patch: str, model_root: str) -> None:
        patch_paths: list[str] = []
        in_hunk = False
        for line in patch.splitlines():
            # Only the metadata of a patch says what it changes, since
            # a hunk body is prose that may read exactly like it. Every
            # body line carries a prefix, which the metadata never has.
            if in_hunk and (not line or line.startswith((" ", "+", "-", "\\"))):
                continue
            in_hunk = line.startswith("@@ ")
            if in_hunk:
                continue
            if line == "GIT binary patch" or line.startswith("Binary files "):
                raise SpecsError("Specification patches cannot contain binary files.")
            if (
                line.startswith(("old mode ", "new mode "))
                or (line.startswith(("new file mode ", "deleted file mode ")) and not line.endswith(" 100644"))
                or (line.startswith("index ") and line.endswith(" 120000"))
            ):
                raise SpecsError("Specification patches cannot change file modes or symlinks.")
            if line.startswith("diff --git "):
                # A file name may hold spaces, and Git leaves it
                # unquoted, so the halves are told apart by the
                # prefixes bounding them rather than by splitting.
                header = line.removeprefix("diff --git ")
                middle = header.find(" b/")
                if not header.startswith("a/") or middle < 0:
                    raise SpecsError("Malformed specification patch path.")
                patch_paths.extend((header[2:middle], header[middle + len(" b/") :]))
            elif line.startswith(("--- ", "+++ ")):
                raw_path = line[4:].split("\t", maxsplit=1)[0]
                if raw_path != "/dev/null":
                    if not raw_path.startswith(("a/", "b/")):
                        raise SpecsError("Malformed specification patch path.")
                    patch_paths.append(raw_path[2:])
            elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
                patch_paths.append(line.split(" ", maxsplit=2)[2])
        if not patch_paths:
            raise SpecsError("Specification patch must change at least one file.")
        for raw_path in patch_paths:
            path = PurePosixPath(raw_path)
            if path.is_absolute() or ".." in path.parts or path.suffix != ".md" or not path.is_relative_to(model_root):
                raise SpecsError(f"Specification patch cannot change `{raw_path}`.")
