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

# A PID is a 32-bit number on both platforms. A larger value identifies no process and is not a JRI record.
MAX_PID = 2**31 - 1

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Workspace:
    # These are the workspace files that stay out of Git. An installation writes them all before it commits.
    # A chat and a run then find each rule already there, and no clone gets this file with a rule missing.
    # A rooted name applies only to the file below the workspace, not to a same-named file in a specification tree.
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

    # A draft states its base. Each resumed run validates that claim with Git before it uses the draft.
    # Refuse a draft that cannot be removed. Report a failed removal instead of raising it.
    # Otherwise, a stopped run could force the user to delete a file that JRI created.
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

    # Return a reset only after every refusal check passes. Check both conditions before return.
    # A `--force` warning then appears only when JRI can perform the reset.

    # A reset can remove data written by two live processes. The window writes notes, session, and logs.
    # A separate runner writes the run directory. Take the project hold and check the runner lock.
    # Removing that directory from a live runner would let the next run start beside it on a separate inode.

    # Hold the project while the caller owns this reset.
    # Another window cannot start, and a runner starts only under this hold.
    # The checks cannot change from no to yes while held. A run that ends then only reduces data at risk.
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
            # Taking it to check would recreate the run directory before this reset removes it.
            if generation_lock.exists() and Lock(generation_lock).is_held():
                raise PersistenceError(
                    "A Just Ralph It run is still going in this project, in a process of its own. A reset takes "
                    "the run directory away from it, and the run after that would start beside it rather than "
                    "after it, so nothing was deleted. Run `jri chat` to watch it or stop it, then try again."
                )
            yield Reset(tuple(path for path in (self.settings_file, *self._reset_paths) if path.exists()))
        finally:
            hold.release()

    # Receive rendered settings instead of loading `Settings`.
    # Finding a workspace never depends on settings loading.
    # A reset requires a `Reset` from `open_reset` under the project hold.
    # A caller cannot request deletion with a flag alone.
    def install(self, settings: str, *, reset: "Reset | None" = None) -> "Installation":
        repository_created = git.find_root(self.root) is None
        repository = Repository.init(self.root)
        created = not self.settings_file.exists()
        if reset is not None:
            self._clear()
        self.directory.mkdir(exist_ok=True, parents=True)
        if created or reset is not None:
            self.settings_file.write_text(settings, encoding="utf-8", newline="\n")
        Notebook(self.notebook_file, self.root.name)
        self.logs_dir.mkdir(exist_ok=True)
        self._ignore()
        # Commit a workspace this installation wrote, and one that no repository here holds yet.
        # An existing workspace can hold notes a chat wrote and settings the user changed.
        # That work belongs to the commit of the turn that made it, not to this one.
        written = created or repository_created or reset is not None
        return Installation(
            self,
            created=created,
            repository_created=repository_created,
            commit=self._commit(repository, reset=reset is not None) if written else None,
        )

    # Commit what the installation wrote. The project then holds its settings, notes, and ignore rules from its
    # first commit, and a clone gets the same workspace.
    # Name the paths. The commit holds no user work, staged or not, and it changes none.
    # Git refuses a partial commit during a merge or a cherry-pick, and a commit off a branch stays reachable only
    # from a detached HEAD. Leave the files in the worktree in both states. The user commits them after that work.
    def _commit(self, repository: Repository, *, reset: bool) -> str | None:
        if not repository.is_on_branch() or repository.has_conflicts() or repository.has_commit("MERGE_HEAD"):
            logger.info("installation_uncommitted")
            return None
        # A workspace can sit under the repository root. Git reads a pathspec from that root.
        prefix = self.root.resolve().relative_to(repository.path)
        installed = tuple((prefix / path).as_posix() for path in paths.INSTALLED_PATHS)
        # A reset removes the specifications of the project it replaces from the disk. Git still holds them and the
        # commit that accepted them, so commit the removal and accept an empty tree. Without both, the first run stops.
        removed = repository.read_staged_paths(((prefix / paths.SPECS_DIR).as_posix(),)) if reset else ()
        committed = (*installed, *removed)
        trailers = (ACCEPTANCE_TRAILER,) if reset else ()
        try:
            # Stage intent only, because Git commits a named path only after the index knows it.
            # Force it, because a project can ignore `.jri`. JRI keeps its workspace in Git anyway.
            # A removed path is already tracked, so it needs no staging.
            repository.stage(installed, intent_to_add=True, force=True)
            # A second installation of an unchanged workspace has nothing to commit.
            # Git reports that state as a failure.
            if not repository.read_status(committed):
                return None
            commit = repository.commit("jri: initialize project", trailers=trailers, paths=committed)
        # A project hook can refuse this commit, and Git can fail for a reason outside JRI.
        # The workspace is written and ready, so report the commit that did not happen instead of ending on it.
        except git.Error:
            logger.exception("installation_commit_failed")
            return None
        logger.info("installation_committed commit=%s", commit)
        return commit

    # These are all paths that `--force` replaces, whether they exist or not. The workspace owns this list.
    @property
    def _reset_paths(self) -> tuple[Path, ...]:
        return tuple(self.root / path for path in paths.RESET_PATHS)

    # A run worktree holds the files Git writes read-only, and Windows refuses to remove one. Remove a directory
    # through `files`, which clears that attribute. A path that still will not go stays, and the reset goes on:
    # the workspace the user asked for is written, and nothing here removes what another process can hold.
    def _clear(self) -> None:
        for path in self._reset_paths:
            if path.is_dir():
                files.remove_directory(path)
            else:
                path.unlink(missing_ok=True)

    # Read and add rules on every call. An existing file can lose a rule after replacement.
    # JRI commits this file, so Git reports a missing rule.
    # A rule inside the hidden directory would hide its own absence.
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


# This is a reset that JRI can perform and the paths it replaces at check time.
# Only `Workspace.open_reset` creates it under the project hold. Its caller has passed both refusal checks.
@dataclass(frozen=True)
class Reset:
    paths: tuple[Path, ...]


class Hold:
    # Hold the claim across lock acquisition and holder recording.
    # A claim held longer than this cannot be waited out here.
    CLAIMED_WITHIN = 1.0
    # A nonresponsive window can still hold the project.
    # A takeover waits for the operating system to release a dead process lock.
    FREED_WITHIN = 5.0
    # The operating system frees the lock of a window it ended, and Windows takes a moment over it. That lock
    # still records the window that left, and the operating system can already have given its number to another
    # process. A lock still held after this long has a window behind it, and a signal reaches that window.
    # Keep this below `FREED_WITHIN`, which the signal needs the rest of to work.
    SIGNALLED_AFTER = 1.0
    POLL = 0.05

    def __init__(self, workspace: Workspace) -> None:
        self.lock = Lock(workspace.root / paths.LOCK_FILE)
        self.claim = Lock(workspace.root / paths.CLAIM_FILE)
        self.holder: int | None = None

    # This states which process holds the project now, and nothing when no process holds it.
    # The record alone is not the answer: only the operating system says whether a holder is alive, and the record
    # stays after its window has left. Take the lock to find that out, and release a lock that this call takes.
    # A project without a lock file has no window, and finding that out must not make one.
    def find_holder(self) -> int | None:
        if not self.lock.path.exists():
            return None
        if self.take():
            self.release()
            return None
        return self.holder

    # This states whether this process holds the project. When it does not, it records the holding JRI PID.
    # The lock proves that the holder is running because the operating system releases it at exit.
    # The lock record identifies the process and is read under its claim.
    def take(self) -> bool:
        if not self._claim():
            raise PersistenceError(
                f"JRI could not find out whether this project is already open: `{self.claim.path}` stayed locked. "
                "Close any other JRI window, then try again."
            )
        try:
            taken = self.lock.take(str(os.getpid()))
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

    # Kill the other window instead of asking it to close. A nonresponsive window cannot process the request.
    # A free lock, not a sent signal, proves that the operating system ended the process.
    def evict(self) -> bool:
        signalled: int | None = None
        started = time.monotonic()
        deadline = started + self.FREED_WITHIN
        # Signal the PID read under the current claim, not a PID read before the user chose eviction.
        # A PID can be reused when its process exits. Signal only the process holding the project now.
        while not self.take():
            holder = self.holder
            if signalled is None and holder is not None and time.monotonic() - started >= self.SIGNALLED_AFTER:
                logger.info("hold_eviction_started holder=%d", holder)
                try:
                    # Signal one process, not its group.
                    # Its runner uses another session, and the user terminal uses this session.
                    os.kill(holder, signal.SIGTERM)
                except OSError:
                    # A window can exit between the lock read and this signal. It then requires no further termination.
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
