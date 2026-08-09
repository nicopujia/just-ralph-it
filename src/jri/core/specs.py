import logging
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
# project held before it, and `indexed` the paths Git already tracked.
# Nothing here says anything about a lock: the file a run leaves in
# `.git` outlives it by however long the project goes unopened, and a
# record cannot tell what took a lock in that time.
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
    # against it, and what it placed is held to what a model writing
    # those same files would be held to. A draft that fails any of this
    # is one no run meets again: it is dropped before anything else can
    # go wrong, because a draft that stops every run and outlives them
    # all would leave the user deleting a file JRI wrote.
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
        self._stage(repository, [destination.relative_to(repository.path).as_posix() for destination in specifications])
        logger.info("specifications_written root=%s files=%d deleted=%d", model_root, len(files), len(deleted))

    # A specification is a plain file, and who is asked decides what
    # that means: to the filesystem a link is an entry a read follows
    # elsewhere, to Git it is a mode, and only the mode survives a
    # platform that makes no links. A Windows checkout writes a
    # `120000` entry out as a plain file holding the target's text,
    # which `Path.is_symlink` answers `no` over -- so a run there would
    # hand a model a path where a specification's body goes, and its
    # acceptance would record the mode straight back, leaving the
    # refusal to fall due on whoever next checks that commit out where
    # links are made. So Git is asked as well, once for the directory
    # being read, and that is where the cost is put: what a run reads
    # here is what a model is shown and what an acceptance commits, and
    # a generation reads a handful of times.
    @staticmethod
    def read(repository: git.Repository, directory: str) -> dict[str, bytes]:
        linked = frozenset(repository.read_staged_paths((directory,), linked=True))
        specifications: dict[str, bytes] = {}
        for path in sorted((repository.path / directory).rglob("*.md")):
            relative = path.relative_to(repository.path).as_posix()
            # A link is a specification at neither end, since it is the
            # text of its target to Git and the target itself to a
            # read, and a directory, a pipe or a socket is one at no
            # end at all. Whichever it is, the run ends over the path
            # inside the tree rather than over the operating system's
            # words about a worktree of JRI's own, whose name is a
            # temporary directory the user never asked for.
            if relative in linked or path.is_symlink() or not path.is_file():
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
        )
        self.workspace.open_generation_dir()
        # Taken for exactly the span this acceptance is under way in,
        # and dropped by the operating system when a kill ends that
        # span, so a run that reads the record back learns whether the
        # run that wrote it is still there without asking a pid the
        # system may have handed on.
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
        if not self.workspace.acceptance_file.exists():
            return
        # A record whose lock is still held is an acceptance under way,
        # and its patch, its index and the record itself are the run
        # carrying it out to finish or to take back. The operating
        # system is what answers whether that run is still there, since
        # it frees the lock when the holder dies and hands on the pid
        # the record could have named instead. Asked before the record
        # is read, so that a read the operating system refuses for a
        # moment is never a live acceptance's own record settled over.
        if Lock(self.workspace.acceptance_lock_file).is_held():
            return
        # Settling writes the index, so a lock still standing here would
        # come back as Git's own words about a path inside `.git`.
        self._check_locks()
        acceptance = self._read_acceptance()
        if acceptance is None:
            self._settle_unreadable_acceptance()
            return
        self._settle_acceptance(acceptance)

    # Whichever Git command meets one of these files next says so and
    # stops, which is a message about a path inside `.git` in the
    # middle of a run about specifications. Naming them is all JRI does
    # with them: a lock file carries no mark of who made it, the
    # operating system frees nothing over it -- Git holds a lock by the
    # file's existence and not by a lock the kernel would drop -- and
    # the acceptance whose leftover it may be died however long before
    # the project was next opened. So a lock a dead run of JRI's left
    # and a lock a Git of the user's is holding this instant are one
    # shape on disk, and the user is the one who can tell which, once
    # they are told which files they are.
    def _check_locks(self) -> None:
        blocking = self.repository.locks.blocking
        if blocking:
            raise RepositoryStateError(
                "Git is locked. Wait for the command holding it, or, if none is running, remove these before "
                "Ralphing:\n" + "\n".join(f"- {path}" for path in blocking)
            )

    # A record JRI cannot read says nothing about the run that wrote
    # it: not what that run applied, not what it staged, not which
    # acceptance commit stood before it. What it is still evidence of
    # is that a run was in the middle of an acceptance, and a truncated
    # write, a corrupted file and a record an older JRI wrote all land
    # here -- so it is settled over rather than taken away, and the
    # settlement is the one below.
    def _read_acceptance(self) -> Acceptance | None:
        try:
            return Acceptance.model_validate_json(self.workspace.acceptance_file.read_bytes())
        except (OSError, ValidationError):
            logger.exception("acceptance_unreadable path=%s", self.workspace.acceptance_file)
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

    # The same settlement with nothing to read it from. Which patch was
    # applied is unknown, so nothing in the worktree is JRI's to take
    # back; which paths the user had staged is unknown, so nothing they
    # staged is JRI's to reset; and which acceptance commit stood
    # before is unknown, so whether this one reached a commit cannot be
    # asked at all -- and reversing one that did would delete
    # specifications the user has. What is left is the step Git itself
    # did not finish, and the worktree answers for that one on its own:
    # a path standing with the very bytes the commit holds is a path
    # only the index disagrees about, whoever wrote it, so putting that
    # index back to the commit takes nothing off the disk and nothing
    # out of any commit. A link the filesystem shows is not such a
    # path, since a read of it gives the target's bytes rather than its
    # own; one only Git holds -- a checkout that had no link to make --
    # is, and putting its entry back to a commit that already records
    # the link leaves the mode standing where `_check_state` names it.
    # Everything else stands where that same check names it, and the
    # record stands with it, an acceptance under way being all it still
    # says; it goes once nothing under the specifications is loose.
    def _settle_unreadable_acceptance(self) -> None:
        settled: list[str] = []
        # A first acceptance dies against a project holding no commit,
        # and there is nothing there for a worktree to agree with.
        if self.repository.has_commit():
            head = self.repository.read_head()
            for entry in self.repository.read_status(paths.COMMITTED_PATHS, ignored=True):
                standing = self.workspace.root / entry.path
                # A path the commit does not hold is one Git answers
                # about with a refusal rather than with bytes.
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
                return self.read(pre_image, paths.SPECS_DIR)
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

    # A draft is the one specification tree that reaches a commit with
    # no answer of a model's behind it: the patch is a file on the
    # user's disk, it outlives the run that composed it, and the JRI
    # reading it is not the JRI that wrote it -- these very rules grew
    # narrower over the series that added the draft, so a draft older
    # than an upgrade can carry a name this JRI refuses to write. So
    # what Git placed is weighed by what `Specs.write` weighs a model's
    # answer by, and a draft carrying what no answer could is refused
    # where the answer would have been. Every entry the tree gained is
    # weighed, not the Markdown alone: a patch places whatever it names,
    # and a file `Specs.read` does not answer for is one no round reads,
    # no commit takes and nothing else here would ever name again --
    # it would simply stand in the user's project, under a directory of
    # JRI's, as something JRI put there. Held against what the checkout
    # put there: a name, a fold or a file the project's own
    # specifications already carry is not this draft's to answer for,
    # and the run meets it either way once the draft is gone.
    @classmethod
    def _check_specifications(
        cls, worktree: Path, standing: Mapping[str, bytes | None], placed: Mapping[str, bytes | None]
    ) -> None:
        prefix = f"{paths.SPECS_DIR}/"
        added = {path.removeprefix(prefix) for path in placed.keys() - standing.keys()}
        for path, content in sorted(placed.items()):
            name = path.removeprefix(prefix)
            # A root is JRI's own word to a model and the draft's own
            # claim here, so the name states which one it is under
            # before it can be weighed against that one.
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

    # What a refused draft placed goes back out, so it costs the run
    # nothing but itself. Git takes back what it can, since a draft can
    # name any path in the worktree and Git holds what stood where JRI
    # read nothing; a draft Git never placed has nothing to take back,
    # and says so by refusing the reverse. The specification tree is put
    # back from the bytes JRI read instead, because how Git ended is not
    # what Git wrote here either: `git apply --reverse` ends at nought
    # over a patch naming one path in two `diff --git` sections having
    # undone the second alone, and the first stands where the run would
    # go on to read it, hand it to a model and commit it. Then the
    # worktree is read back against what the checkout left, since a
    # restore asserts as much as an apply does, and a worktree JRI
    # cannot account for is one no round may write onto -- the draft is
    # already gone by then, so the run after this one starts clean. What
    # is taken away is this run's own: the worktree is the one
    # `open_worktree` made for this run, under a temporary directory
    # this run named and removes as it ends.
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
        # Whatever stopped the restore part way, what the worktree holds
        # is the question, and the read below is what asks it.
        except (OSError, git.Error):
            logger.exception("draft_restore_failed worktree=%s", repository.path)
        if self._read_specification_tree(repository.path) != standing or repository.read_status() != status:
            raise SpecsError(
                "JRI could not take a drafted specification back out of the worktree it was writing in, so nothing "
                "was committed. Your project keeps the specifications it already had. Try again."
            )

    # Every entry the tree holds that `standing` does not, taken away;
    # every one it holds different bytes for, written again. What comes
    # back is the paths that moved, for the index to be told about. An
    # entry `standing` has no bytes for is one JRI never read, so it is
    # nobody's here to write over.
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

    # Every entry standing under the specification tree, with the bytes
    # that would put it back where JRI can read them and `None` where it
    # cannot -- a link, a socket, a file the operating system refuses.
    # `Specs.read` answers what a specification is; this answers what is
    # there, so that a restore takes away what the checkout did not
    # leave and leaves alone what it did.
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

    # A file Git does not track is one `git diff` says nothing about, so
    # every path a write of JRI's touched goes into the index the
    # acceptance's diff is read against. `git add` refuses a whole
    # command over one path that names nothing, and a deletion of a file
    # Git never held is such a path with nothing to record besides -- so
    # what is staged is what Git can see: the files the write left on
    # disk, and the entries it took away from under Git. Forced, since
    # `.jri` is JRI's to keep in Git whatever the project's ignore rules
    # say about it.
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
        # Both are asked, because a link is a shape on disk to one and
        # a mode to the other, and neither answer covers the other's
        # ground: the filesystem holds a link Git never heard of, and
        # Git holds one where the platform makes none -- a Windows
        # checkout leaves a `120000` entry standing as a plain file,
        # which the run would read as a specification, hand to a model
        # as its body and commit the mode straight back over. The
        # filesystem is asked about `COMMITTED_PATHS` as it spells
        # them, and Git about the same paths as Git does.
        committed = (
            self.workspace.config_file,
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
