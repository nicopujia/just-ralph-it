import logging
import os
import re
from collections.abc import Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager, suppress
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
# What a name inside the specification tree may be made of, allowed
# rather than forbidden, so a character nothing here names is a
# character no name holds. Such a name is a file on whichever machine
# the project is cloned onto and a Git pathspec wherever JRI stages
# it, and it answers to both at once: Windows refuses the control
# characters and `<>:"/\|?*` outright and strips a trailing space or
# dot off what is left, and Git reads `*?[]\` in a pathspec as a
# pattern rather than as the file it names. The tree is JRI's own
# machinery under two roots JRI named in English, so ASCII costs it
# nothing -- what the project is written in lives in the body, which
# this says nothing about.
SPECIFICATION_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._-]*[A-Za-z0-9_-])?")
# Windows resolves each of these to a device however it is cased and
# whatever extension follows it, so no file can carry one as a name.
WINDOWS_DEVICE_NAMES = frozenset({
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{port}" for port in "123456789"),
    *(f"LPT{port}" for port in "123456789"),
})

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
# project held before it, `indexed` the paths Git already tracked,
# `locked` the locks Git already held, so one this acceptance goes on
# to leave is told from one that was there before it, and `pid` the
# run carrying it out, so a lock left in `.git` beside this is told
# from one the run reading it is holding right now.
class Acceptance(BaseModel):
    accepted: str | None
    patch: str
    indexed: tuple[str, ...]
    locked: tuple[str, ...]
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

    # Whether a run before this one left specifications it never got to
    # commit. Nothing is recorded beside the draft to say so: the file
    # is there or it is not, and what it holds is weighed by Git.
    @property
    def drafted(self) -> bool:
        return self.workspace.draft_file.exists()

    # A draft says one thing -- `I am what a run wrote onto the
    # specifications the project holds` -- and Git is what puts that to
    # the test: the whole patch is weighed before any of it is written,
    # so a draft the specifications have moved past leaves the worktree
    # exactly as the checkout left it. How Git ended is not what Git
    # wrote, so what comes back is the tree read either side of the
    # apply and weighed against itself: a draft none of which reached
    # the specifications is one no run picked up, whatever Git's ending
    # said, and the delta is the draft rather than the tree the
    # checkout placed under it. What Git can place may still not be a
    # specification tree JRI can read back, since a patch nothing of
    # JRI's wrote can put a link where a specification goes, so the
    # tree is read here rather than by the round that would write
    # against it. A draft that fails any of this is one no run meets
    # again: it is dropped before anything else can go wrong, because a
    # draft that stops every run and outlives them all would leave the
    # user deleting a file JRI wrote.
    def resume(self, repository: git.Repository) -> tuple[str, ...] | None:
        draft = self._read_draft()
        try:
            checked_out = self.read(repository.path, paths.SPECS_DIR)
            repository.apply_patch(draft, index=True)
            placed = self.read(repository.path, paths.SPECS_DIR)
            drafted = tuple(
                path for path in sorted(checked_out.keys() | placed.keys()) if checked_out.get(path) != placed.get(path)
            )
        except (git.Error, SpecsError):
            logger.info("draft_refused characters=%d", len(draft))
        else:
            if drafted:
                return drafted
            logger.info("draft_placed_nothing characters=%d", len(draft))
        self.workspace.drop_draft()
        # Whatever Git placed goes back out, so a refused draft costs
        # the run nothing but itself. A draft Git never placed has
        # nothing to take back, and says so by refusing the reverse.
        with suppress(git.Error):
            repository.apply_patch(draft, index=True, reverse=True)
        return None

    def write(
        self, repository: git.Repository, files: Mapping[str, str], deleted: Sequence[str], model_root: str
    ) -> None:
        if not files and not deleted:
            raise SpecsError("Specifications must change at least one file.")
        # A null character is what makes Git read a file as binary, and
        # a binary file's diff names it and carries none of its
        # content, which `git apply` refuses. So the run would end
        # blaming a write of JRI's for text a model returned.
        binary = next((path for path, content in sorted(files.items()) if "\x00" in content), None)
        if binary is not None:
            raise SpecsError(f"Specifications are text, and `{binary}` holds a null character.")
        root = repository.path / paths.SPECS_DIR
        # A path named on both sides is a file the model both wrote and
        # removed, and the removal is the later word on it.
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
                # Removed rather than opened, so a link standing where a
                # specification goes is what JRI writes over instead of
                # what it writes through.
                destination.unlink(missing_ok=True)
                if content is not None:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(content, encoding="utf-8", newline="")
            # What the filesystem refuses a path for -- a name it cannot
            # hold, a directory where a file stands -- is a fact about
            # the path a model returned, so the run ends naming it
            # rather than unwinding as a fault of JRI's own.
            except (OSError, ValueError) as error:
                logger.exception("specification_write_failed path=%s", destination)
                raise SpecsError(
                    f"JRI could not write the specification `{destination.relative_to(root).as_posix()}` it "
                    "drafted. Nothing was committed. Your notes stand, and your project keeps the "
                    "specifications it already had."
                ) from error
        # A file Git does not track is one `git diff` says nothing
        # about, so every path this write touched goes into the index
        # the acceptance's diff is read against. `git add` refuses a
        # whole command over one path that names nothing, and a
        # deletion of a file Git never held is such a path with nothing
        # to record besides -- so what is staged is what Git can see:
        # the files this write left on disk, and the entries it took
        # away from under Git. Forced, since `.jri` is JRI's to keep in
        # Git whatever the project's ignore rules say about it.
        touched = [destination.relative_to(repository.path).as_posix() for destination in specifications]
        staged = sorted(
            {path for path in touched if (repository.path / path).is_file()}
            | set(repository.read_staged_paths(touched))
        )
        if staged:
            repository.stage(staged, force=True)
        logger.info("specifications_written root=%s files=%d deleted=%d", model_root, len(files), len(deleted))

    @staticmethod
    def read(worktree: Path, directory: str) -> dict[str, bytes]:
        specifications: dict[str, bytes] = {}
        for path in sorted((worktree / directory).rglob("*.md")):
            relative = path.relative_to(worktree).as_posix()
            # A specification is a plain file: a link is the text of
            # its target to Git and the target itself to a read, so it
            # is a specification at neither end, and a directory, a
            # pipe or a socket is one at no end at all. Whichever it
            # is, the run ends over the path inside the tree rather
            # than over the operating system's words about a worktree
            # of JRI's own, whose name is a temporary directory the
            # user never asked for.
            if path.is_symlink() or not path.is_file():
                raise SpecsError(f"JRI writes plain specification files, and `{relative}` is not one.")
            try:
                specifications[relative] = path.read_bytes()
            except OSError as error:
                logger.exception("specification_read_failed path=%s", relative)
                raise SpecsError(f"JRI could not read the specification `{relative}`: {error.strerror}") from error
        return specifications

    @staticmethod
    def render(files: dict[str, bytes]) -> str:
        prefix = f"{paths.SPECS_DIR}/"
        rendered: list[str] = []
        for path, content in sorted(files.items()):
            name = path.removeprefix(prefix)
            try:
                body = content.decode()
            # Everything JRI writes here is UTF-8, and Git hands back
            # whatever a commit holds, so bytes that are not are bytes
            # JRI did not write. Decoding them for a model is a choice
            # about what they say, which is the user's to make.
            except UnicodeDecodeError as error:
                raise SpecsError(f"Specifications are UTF-8 text, and `{name}` is not.") from error
            # A model named the file as much as it wrote the body: an
            # rglob over the specification tree named the path, and
            # the model named what that rglob had to find. So the name
            # is quoted for the reason the body is -- as prose, a name
            # carrying a line break writes a second `File:` entry,
            # with a body of its own, inside the one block JRI is the
            # author of.
            rendered.append(prompt.render(file=name, content=body))
        return "\n\n".join(rendered) or "(empty)"

    # The run's work so far, written down where the next run will look
    # for it and handed back as the patch this one would commit. Git
    # composes it, so what is kept is a delta onto the specifications
    # the project holds rather than anything a model said about one --
    # and a run whose specifications are the ones already committed has
    # composed nothing, which is a draft to take away rather than an
    # empty file for the next run to make sense of.
    def save_draft(self, repository: git.Repository, baseline: Baseline) -> bytes:
        patch = repository.diff(baseline.commit, paths=(paths.FUNCTIONAL_SPECS_DIR, paths.ARCHITECTURE_SPECS_DIR))
        if not patch:
            self.workspace.drop_draft()
            return patch
        # The directory answers for itself in the ignore file JRI
        # commits, so the draft is out of `git add -A`, out of the copy
        # the repository study runs in, and out of the tree the
        # architect is shown, from the first run that writes one.
        self.workspace.open_generation_dir()
        try:
            files.write_atomically(self.workspace.draft_file, patch.decode())
            logger.info("draft_saved characters=%d", len(patch))
        # Keeping the work is what the draft is for, and a run stopped
        # over a place to keep it would be a run that lost the work
        # outright -- and so would every run after it, since nothing
        # about the place would have changed.
        except OSError:
            logger.exception("draft_write_failed path=%r", self.workspace.draft_file)
        return patch

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
            locked=tuple(str(path) for path in self.repository.locks.standing),
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
                commit = self._settle_acceptance(acceptance)
                if commit is None:
                    raise
            else:
                self._drop_acceptance()
        # The commit is what the draft was working towards, so it is
        # what spends it: from here the project holds those
        # specifications and a run resuming the delta onto them would
        # be writing them twice.
        self.workspace.drop_draft()
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
        self._release_locks(acceptance)
        # Undoing writes the index, so a lock still standing here would
        # come back as Git's own words about a path inside `.git`.
        self._check_locks()
        self._settle_acceptance(acceptance)

    # Git's own guard against two commands writing one file at once,
    # so JRI takes one away only where it can say whose it is: a
    # record says an acceptance was under way and names the locks Git
    # already held when it opened, so a lock standing now that is not
    # among them is one that acceptance left; a pid that is not this
    # one says the run doing it was another; and a lock the operating
    # system has already dropped says that run is dead. Anything else
    # is a lock JRI cannot account for, and `_check_locks` names it
    # instead.
    def _release_locks(self, acceptance: Acceptance) -> None:
        if acceptance.pid == os.getpid() or Lock(self.workspace.acceptance_lock_file).is_held():
            return
        self.repository.locks.release([Path(path) for path in acceptance.locked])

    # Whichever Git command meets one of these files next says so and
    # stops, which is a message about a path inside `.git` in the
    # middle of a run about specifications. What a killed acceptance of
    # JRI's left is already gone by here, so what stands is either a
    # command running now or one nothing of JRI's accounts for -- and
    # either way the user is the one who can tell which, once they are
    # told which files they are.
    def _check_locks(self) -> None:
        blocking = self.repository.locks.blocking
        if blocking:
            raise RepositoryStateError(
                "Git is locked. Wait for the command holding it, or, if none is running, remove these before "
                "Ralphing:\n" + "\n".join(f"- {path}" for path in blocking)
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

    # How Git's command ended is not what Git wrote: a death past the
    # reference transaction -- an out-of-memory kill, a `pkill git`, a
    # hook of the project's whose Git is killed -- comes back non-zero
    # over a commit that is written, and a kill of the whole run comes
    # back as nothing at all. So both are answered the same way, by
    # asking the project which commit carries the trailer rather than
    # asking Git how it went, and a commit that is there is the
    # project's from then on: reversing its patch would delete
    # specifications the user has, and leave every run after it
    # refusing over the deletion. One question, whose own ending is
    # either the answer or an error the run ends on: a second question
    # in front of it would answer `the project holds no commit` for a
    # Git that was killed, which is this same mistake one command
    # further back.
    def _settle_acceptance(self, acceptance: Acceptance) -> str | None:
        accepted = self.repository.find_commit(ACCEPTANCE_TRAILER)
        if accepted == acceptance.accepted:
            self._undo_acceptance(acceptance)
            return None
        # A commit of named paths is written from an index of Git's own
        # and only then copied over the one the project keeps, so a
        # death between the two leaves the commit holding what JRI
        # staged and the index holding the intent to add it -- every
        # specification in the commit reported deleted, which stops
        # every run after. Git's own last step, taken here because Git
        # did not reach it: past the commit, what the index holds for a
        # path of JRI's is what the commit holds.
        if accepted is not None:
            self.repository.unstage(paths.COMMITTED_PATHS)
        self._drop_acceptance()
        logger.info("acceptance_committed commit=%s", accepted)
        return accepted

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
    # and neither does one whose tree holds something JRI cannot read
    # back as a specification; an undo with nothing to say leaves every
    # leftover standing.
    def _rebuild_writes(self, acceptance: Acceptance) -> dict[str, bytes] | None:
        try:
            with self._open_pre_image(acceptance.accepted) as pre_image:
                pre_image.apply_patch(acceptance.patch.encode())
                return self.read(pre_image.path, paths.SPECS_DIR)
        except (git.Error, SpecsError):
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

    # A draft nothing can read says nothing, and neither does one
    # holding no bytes: both come back as the empty patch, which Git
    # refuses like any other draft it cannot place, so a run that meets
    # one is told the draft no longer fits rather than left wondering
    # whether it was picked up.
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
        # What `_locate_specification` refuses a model's path for, the
        # files JRI commits may not be either. Git records a link as
        # the text of its target, so the notebook comes back out of the
        # commit as a path where the notes should be, and a
        # specification read out of a worktree is whatever the link
        # points at -- a file that was never JRI's to show a model.
        # These are `COMMITTED_PATHS` again, as the filesystem rather
        # than Git spells them.
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

    # Two names a filesystem reads without case are one file there and
    # two here, and the tree is committed for every machine to check
    # out. So a generation naming both leaves a repository whose
    # specifications Windows and macOS cannot hold as written, and a
    # rename that only changes case is one JRI cannot carry out at all:
    # the write and the removal land on the same file, and the removal
    # is second. What answers `*.md` in the root already stands beside
    # what this write names, since either side can be the other's pair,
    # and both are weighed as text: a `Path` is the one a filesystem
    # that ignores case would fold them into, on a machine whose does.
    @staticmethod
    def _find_folded_names(root: Path, model_root: str, written: Iterable[str]) -> tuple[str, str] | None:
        standing = {path.relative_to(root).as_posix() for path in (root / model_root).rglob("*.md")}
        found: dict[str, str] = {}
        for name in sorted(standing | {PurePosixPath(path).as_posix() for path in written}):
            first = found.setdefault(name.lower(), name)
            if first != name:
                return first, name
        return None

    # Where a path a model returned lands, and every bound such a path
    # answers to: a Markdown file inside the model's own root, named as
    # `_names_a_file` spells out, reached without following a link. A
    # file a model writes is held to all of this here, and nowhere
    # else.
    @classmethod
    def _locate_specification(cls, worktree: Path, raw_path: str, model_root: str) -> Path:
        path = PurePosixPath(raw_path)
        destination = worktree / paths.SPECS_DIR / path
        try:
            # A link anywhere between the worktree and the file answers
            # to none of the rules below, which read the path and not
            # the disk it names, and the bound is spelled out from the
            # worktree Git itself made so that a link standing where a
            # directory of JRI's own goes is caught here too. A name
            # the filesystem will not even be asked about is no more a
            # specification than one that leaves the root.
            located = destination.resolve().parent.is_relative_to(worktree.resolve() / paths.SPECS_DIR / model_root)
        except (OSError, ValueError):
            located = False
        if not cls._names_a_file(path) or path.suffix != ".md" or not path.is_relative_to(model_root) or not located:
            raise SpecsError(f"Specifications cannot change `{raw_path}`.")
        return destination

    # What every part of such a path has to be: not a traversal, not
    # the root of a filesystem, not a directory a specification glob
    # answers -- `Specs.read` reads back whatever answers `*.md`, and
    # that glob ignores case wherever the filesystem does, so `notes.MD`
    # is one such directory on Windows and `notes.md` is one everywhere
    # -- and a name each of the three platforms will hold and Git will
    # read as the file it is rather than as a pathspec pattern.
    @staticmethod
    def _names_a_file(path: PurePosixPath) -> bool:
        if path.is_absolute() or ".." in path.parts or any(part.lower().endswith(".md") for part in path.parts[:-1]):
            return False
        return bool(path.parts) and all(
            SPECIFICATION_NAME.fullmatch(part) is not None
            and part.partition(".")[0].upper() not in WINDOWS_DEVICE_NAMES
            for part in path.parts
        )
