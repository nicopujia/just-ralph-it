import logging
import os
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from jri.lib import files, git
from jri.lib.lock import Lock

from . import paths
from .exceptions import PersistenceError
from .notes import Notebook
from .repository import ACCEPTANCE_TRAILER, Repository

if TYPE_CHECKING:
    from collections.abc import Generator

# A pid is a 32-bit number on both platforms. A larger value identifies no process and is not a JRI record.
MAX_PID = 2**31 - 1

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Workspace:
    # These are the workspace files that stay out of Git. JRI writes all these rules before it commits.
    # A chat and a run then find each rule already there, and no clone gets this file with a rule missing.
    # A rooted name applies only to the file below the workspace.
    # It does not apply to a file with the same name in a specification tree.
    # A slash ends a directory name.
    WORKSPACE_IGNORES: ClassVar[tuple[str, ...]] = (
        Path(paths.SESSION_FILE).name,
        Path(paths.LOGS_DIR).name,
        Path(paths.VISUALIZATION_FILE).name,
        f"/{Path(paths.LOCK_FILE).name}",
        f"/{Path(paths.CLAIM_FILE).name}",
        f"/{Path(paths.GENERATION_DIR).name}/",
        f"/{Path(paths.WORKTREE_DIR).name}/",
    )

    root: Path

    @staticmethod
    def find() -> "Workspace":
        cwd = Path.cwd()
        return Workspace(git.find_root(cwd) or cwd)

    @property
    def directory(self) -> Path:
        return self.root / paths.WORKSPACE_DIR

    @property
    def settings_file(self) -> Path:
        return self.root / paths.SETTINGS_FILE

    @property
    def gitignore_file(self) -> Path:
        return self.root / paths.GITIGNORE_FILE

    @property
    def notebook_file(self) -> Path:
        return self.root / paths.NOTEBOOK_FILE

    @property
    def session_file(self) -> Path:
        return self.root / paths.SESSION_FILE

    @property
    def visualization_file(self) -> Path:
        return self.root / paths.VISUALIZATION_FILE

    @property
    def logs_dir(self) -> Path:
        return self.root / paths.LOGS_DIR

    @property
    def log_file(self) -> Path:
        return self.root / paths.LOG_FILE

    @property
    def log_lock_file(self) -> Path:
        return self.root / paths.LOG_LOCK_FILE

    @property
    def generation_dir(self) -> Path:
        return self.root / paths.GENERATION_DIR

    @property
    def acceptance_file(self) -> Path:
        return self.root / paths.ACCEPTANCE_FILE

    @property
    def acceptance_lock_file(self) -> Path:
        return self.root / paths.ACCEPTANCE_LOCK_FILE

    @property
    def draft_file(self) -> Path:
        return self.root / paths.DRAFT_FILE

    # A draft states its base. Each run that resumes validates that claim with Git before it uses the draft.
    # Refuse a draft that JRI cannot remove. Write that failure to the log instead of raising an error.
    # If JRI raised an error, a run that stopped could force the user to delete a file that JRI made.
    def drop_draft(self) -> None:
        try:
            self.draft_file.unlink(missing_ok=True)
        except OSError:
            logger.exception("draft_removal_failed path=%r", self.draft_file)

    # One chat writes the notes, conversation, and run. Only one chat can hold the project.
    # Never delete these files because a live window can hold either one.
    def open_hold(self) -> "Hold":
        self.directory.mkdir(exist_ok=True, parents=True)
        self._ignore()
        return Hold(self)

    # This directory holds run data while it works. It never holds committed data.
    def open_generation_dir(self) -> Path:
        self.generation_dir.mkdir(exist_ok=True, parents=True)
        self._ignore()
        return self.generation_dir

    # Git creates the run worktree at this path. Write the ignore rule first, because Git reads a worktree here
    # as project content without it.
    def reserve_worktree_dir(self) -> Path:
        self.directory.mkdir(exist_ok=True, parents=True)
        self._ignore()
        return self.root / paths.WORKTREE_DIR

    # Return a reset only after every refusal check passes. Make both checks before you return.
    # A `--force` warning then appears only when JRI can perform the reset.

    # A reset can remove data that two live processes wrote. The window writes notes, session, and logs.
    # A separate runner writes the run directory. Take the project hold and check the runner lock.
    # If JRI removed that directory from a live runner, the next run would start beside it on a separate inode.

    # Hold the project while the caller owns this reset.
    # Another window cannot start, and a runner starts only under this hold.
    # While JRI holds the project, the checks cannot change from no to yes.
    # A run that ends then only decreases the data at risk.
    @contextmanager
    def open_reset(self) -> "Generator[Reset]":
        generation_lock = self.root / paths.GENERATION_LOCK_FILE
        hold = self.open_hold()
        if not hold.take():
            raise PersistenceError(
                f"Another JRI is already open in this project, in the window running process {hold.holder}. "
                "It is still writing the notes and the conversation a reset deletes, so nothing was deleted. "
                "Close that window, then try again."
            )
        try:
            # A missing lock file has no holder.
            # A check that took the lock would make the run directory again before this reset removes it.
            if generation_lock.exists() and Lock(generation_lock).is_held():
                raise PersistenceError(
                    "A Just Ralph It run is still going in this project, in a process of its own. A reset takes "
                    "the run directory away from it, and the run after that would start beside it rather than "
                    "after it, so nothing was deleted. Run `jri chat` to watch it or stop it, then try again."
                )
            yield Reset(tuple(path for path in (self.settings_file, *self._reset_paths) if path.exists()))
        finally:
            hold.release()

    # Receive rendered settings. Do not load `Settings` here.
    # JRI must find a workspace even when it cannot load the settings.
    # A reset requires a `Reset` from `open_reset` under the project hold.
    # A caller cannot ask JRI to delete files with a flag alone.
    def install(self, settings: str, *, reset: "Reset | None" = None) -> "Installation":
        repository_created = git.find_root(self.root) is None
        repository = Repository.init(self.root)
        created = not self.settings_file.exists()
        if reset is not None:
            self._clear()
        try:
            self._write(settings, write_settings=created or reset is not None)
        # The directory can refuse a write, and a file can stand where JRI writes a directory.
        except OSError as error:
            raise PersistenceError(f"Could not write the workspace at `{self.directory}`: {error.strerror}") from error
        # Commit a workspace this installation wrote, and one that no repository here holds yet.
        # An existing workspace can hold notes a chat wrote and settings the user changed.
        # That work belongs to the commit of the turn that made it, not to this one.
        written = created or repository_created or reset is not None
        commit, refusal = self._commit(repository, reset=reset is not None) if written else (None, "")
        return Installation(
            self, created=created, repository_created=repository_created, commit=commit, refusal=refusal
        )

    # Write the files of a workspace. Write the settings file only when the caller asks for it.
    def _write(self, settings: str, *, write_settings: bool) -> None:
        self.directory.mkdir(exist_ok=True, parents=True)
        if write_settings:
            self.settings_file.write_text(settings, encoding="utf-8", newline="\n")
        Notebook(self.notebook_file, self.root.name)
        self.logs_dir.mkdir(exist_ok=True)
        self._ignore()

    # Commit what the installation wrote. The project then holds its settings, notes, and ignore rules from its
    # first commit, and a clone gets the same workspace.
    # Name the paths. The commit holds no user work, staged or not, and it changes none.
    # Git refuses a partial commit during a merge or a cherry-pick.
    # A commit that JRI makes outside a branch stays reachable only from a detached HEAD.
    # Leave the files in the worktree in both states. The user commits them after that work.
    def _commit(self, repository: Repository, *, reset: bool) -> tuple[str | None, str]:
        if not repository.is_on_branch() or repository.has_conflicts() or repository.has_commit("MERGE_HEAD"):
            logger.info("installation_uncommitted")
            return None, ""
        # A workspace can be below the repository root. Git reads a pathspec from that root.
        prefix = self.root.resolve().relative_to(repository.path)
        installed = tuple((prefix / path).as_posix() for path in paths.INSTALLED_PATHS)
        # A reset removes from the disk the specifications of the project that it replaces.
        # Git still holds those specifications and the commit that accepted them.
        # Commit the removal and accept an empty tree. Without both, the first run stops.
        removed = repository.read_staged_paths(((prefix / paths.SPECS_DIR).as_posix(),)) if reset else ()
        committed = (*installed, *removed)
        trailers = (ACCEPTANCE_TRAILER,) if reset else ()
        try:
            # Stage intent only, because Git commits a named path only after the index knows it.
            # Force it, because a project can ignore `.jri`. JRI keeps its workspace in Git anyway.
            # A removed path is already tracked, so JRI does not stage it.
            repository.stage(installed, intent_to_add=True, force=True)
            # A second installation of an unchanged workspace has nothing to commit.
            # Git reports that state as a failure.
            if not repository.read_status(committed):
                return None, ""
            commit = repository.commit("jri: initialize project", trailers=trailers, paths=committed)
        # A project hook can refuse this commit, and Git can fail for a reason outside JRI.
        # JRI wrote the workspace and it is ready.
        # Name the commit that did not occur instead of raising an error.
        except git.Error as error:
            logger.exception("installation_commit_failed")
            return None, str(error)
        logger.info("installation_committed commit=%s", commit)
        return commit, ""

    # These are all paths that `--force` replaces, whether they exist or not. The workspace owns this list.
    @property
    def _reset_paths(self) -> tuple[Path, ...]:
        return tuple(self.root / path for path in paths.RESET_PATHS)

    # A run worktree holds the files that Git writes read-only, and Windows refuses to remove such a file.
    # Remove a directory through `files`, which clears that attribute.
    # If JRI still cannot remove a path, leave that path and continue the reset.
    # JRI then writes the workspace that the user asked for.
    # Nothing here removes a file that another process can hold.
    def _clear(self) -> None:
        for path in self._reset_paths:
            if path.is_dir():
                files.remove_directory(path)
            else:
                path.unlink(missing_ok=True)

    # Read and add the rules on every call. An existing file can lose a rule when a user replaces it.
    # JRI commits the project `.gitignore`, so Git reports a rule that goes missing.
    # A `.gitignore` inside the hidden directory would be ignored itself, and nothing would report the loss.
    def _ignore(self) -> None:
        content = self.gitignore_file.read_text(encoding="utf-8") if self.gitignore_file.exists() else ""
        missing = [pattern for pattern in self.WORKSPACE_IGNORES if pattern not in content.splitlines()]
        if not missing:
            return
        separator = "" if not content or content.endswith("\n") else "\n"
        self.gitignore_file.write_text(f"{content}{separator}{'\n'.join(missing)}\n", encoding="utf-8", newline="\n")


@dataclass(frozen=True)
class Installation:
    workspace: Workspace
    created: bool
    repository_created: bool
    commit: str | None
    # Git names why it did not accept the commit. An installation that Git accepted names nothing.
    refusal: str = ""


# This is a reset that JRI can perform. It holds the paths that it replaces, as they were at the time of the check.
# Only `Workspace.open_reset` creates it under the project hold. Its caller has passed both refusal checks.
@dataclass(frozen=True)
class Reset:
    paths: tuple[Path, ...]


class Hold:
    # Hold the claim while JRI takes the lock and writes the holder.
    # JRI does not wait here for another process that holds the claim longer than this time.
    CLAIMED_WITHIN = 1.0
    # A window that does not answer can still hold the project.
    # JRI waits this long for the operating system to release the lock of a process that ended.
    FREED_WITHIN = 5.0
    # The operating system frees the lock of a window that it ended, and Windows needs a short time to do it.
    # That lock still records the window that left.
    # The operating system can also have given the number of that window to another process.
    # A process that still holds the lock after this time is a live window, and a signal reaches it.
    # Keep this value below `FREED_WITHIN`, because the signal needs the remaining time to work.
    SIGNALLED_AFTER = 1.0
    POLL = 0.05

    def __init__(self, workspace: Workspace) -> None:
        self.lock = Lock(workspace.root / paths.LOCK_FILE)
        self.claim = Lock(workspace.root / paths.CLAIM_FILE)
        self.holder: int | None = None

    # This states which process holds the project now. It states nothing when no process holds it.
    # The record alone is not the answer. Only the operating system says if a holder is alive.
    # The record also stays after its window left.
    # Take the lock to find this out, and release a lock that this call takes.
    # A project without a lock file has no window, and this check must not make a lock file.
    def find_holder(self) -> int | None:
        if not self.lock.path.exists():
            return None
        # Take the lock, but write no record of this process.
        # A reader only tells what holds the project now.
        # It must leave the record for the window that takes the project next.
        if self._take(""):
            self.release()
            return None
        return self.holder

    # This states if this process holds the project. If it does not, this records the pid of the JRI that holds it.
    # The lock proves that the holder is running, because the operating system releases the lock at exit.
    # The lock record identifies the process, and JRI reads that record under the claim.
    def take(self) -> bool:
        return self._take(str(os.getpid()))

    # Kill the other window. Do not ask it to close, because a window that does not answer cannot obey.
    # A free lock proves that the operating system ended the process. A signal that JRI sent does not prove it.
    def evict(self) -> bool:
        signalled: int | None = None
        started = time.monotonic()
        deadline = started + self.FREED_WITHIN
        # Signal the pid that JRI reads under the current claim.
        # Do not signal a pid that JRI read before the user chose to evict the window.
        # The operating system can give a pid to another process after the first process exits.
        # Signal only the process that holds the project now.
        while not self.take():
            holder = self.holder
            if signalled is None and holder is not None and time.monotonic() - started >= self.SIGNALLED_AFTER:
                logger.info("hold_eviction_started holder=%d", holder)
                try:
                    # Signal one process, not its group.
                    # Its runner uses another session, and the user terminal uses this session.
                    os.kill(holder, signal.SIGTERM)
                except OSError:
                    # A window can exit between the moment JRI reads the lock and the moment it sends this signal.
                    # JRI then does not have to end that window.
                    logger.exception("hold_kill_failed holder=%d", holder)
                signalled = holder
            if time.monotonic() >= deadline:
                logger.info("hold_eviction_failed holder=%r", self.holder)
                return False
            time.sleep(self.POLL)
        logger.info("hold_eviction_finished signalled=%r", signalled)
        return True

    def release(self) -> None:
        self.lock.release()

    def _claim(self) -> bool:
        deadline = time.monotonic() + self.CLAIMED_WITHIN
        while not self.claim.take():
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.POLL)
        return True

    # Take the lock under the claim, and read the record of the holder that refused it.
    # A caller that only reads passes no record of its own.
    def _take(self, holder: str) -> bool:
        if not self._claim():
            raise PersistenceError(
                f"JRI could not find out whether this project is already open: `{self.claim.path}` stayed locked. "
                "Close any other JRI window, then try again."
            )
        try:
            taken = self.lock.take(holder)
            record = "" if taken else self.lock.holder
        finally:
            self.claim.release()
        if taken:
            self.holder = None
            return True
        if not record.isdigit() or int(record) > MAX_PID:
            raise PersistenceError(
                f"Something holds `{self.lock.path}` without saying what it is, so JRI will not end it. "
                "Close any other JRI window, then try again."
            )
        self.holder = int(record)
        logger.info("hold_refused holder=%d", self.holder)
        return False
