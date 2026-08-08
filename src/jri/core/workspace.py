import logging
import os
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from jri.lib import git
from jri.lib.lock import Lock

from . import paths
from .exceptions import PersistenceError
from .notes import Notebook
from .repository import Repository

# A pid is a 32-bit number on both platforms, so a record naming
# anything larger names no process and is not a record JRI wrote.
MAX_PID = 2**31 - 1

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Workspace:
    PROJECT_IGNORES: ClassVar[tuple[str, ...]] = (".DS_Store", ".env", ".env.*")

    root: Path

    @staticmethod
    def find() -> "Workspace":
        cwd = Path.cwd()
        return Workspace(git.find_root(cwd) or cwd)

    @property
    def directory(self) -> Path:
        return self.root / paths.WORKSPACE_DIR

    @property
    def config_file(self) -> Path:
        return self.root / paths.CONFIG_FILE

    @property
    def gitignore_file(self) -> Path:
        return self.root / paths.GITIGNORE_FILE

    @property
    def project_gitignore_file(self) -> Path:
        return self.root / paths.PROJECT_GITIGNORE_FILE

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

    @property
    def reset_paths(self) -> tuple[Path, ...]:
        return tuple(self.root / path for path in paths.RESET_PATHS)

    # A draft states what it is a delta onto, and every run that picks
    # one up puts that claim to Git before believing it, so a draft
    # nothing can take away is refused rather than obeyed. That is why
    # this reports a removal it could not make instead of raising one:
    # a run stopped over a file JRI itself wrote would have no way out
    # but the user deleting it.
    def drop_draft(self) -> None:
        try:
            self.draft_file.unlink(missing_ok=True)
        except OSError:
            logger.exception("draft_removal_failed path=%r", self.draft_file)

    # One chat writes the notes, the conversation and the run, so one
    # chat at a time has the project. Neither file is ever deleted:
    # they are the two a live window may be holding this instant.
    def open_hold(self) -> "Hold":
        self.directory.mkdir(exist_ok=True, parents=True)
        # Rooted and named one by one, so the rules answer for these
        # two files and for nothing a specification tree happens to
        # hold under the same names.
        self._ignore(*(f"/{Path(path).name}" for path in (paths.LOCK_FILE, paths.CLAIM_FILE)))
        return Hold(self)

    # What a run writes down while it works, and never what it commits.
    def open_generation_dir(self) -> Path:
        self.generation_dir.mkdir(exist_ok=True, parents=True)
        # Rooted at the workspace and closed by a slash, so the rule
        # answers for this directory and for no `generation` a
        # specification tree happens to hold.
        self._ignore(f"/{self.generation_dir.name}/")
        return self.generation_dir

    # The rendered configuration comes in rather than being read from
    # `Settings`, so locating a workspace never depends on loading one.
    def install(self, config: str, *, force: bool = False) -> "Installation":
        repository_created = git.find_root(self.root) is None
        Repository.init(self.root)
        created = not self.config_file.exists()
        if force:
            for path in self.reset_paths:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
        self.directory.mkdir(exist_ok=True, parents=True)
        if created or force:
            self.config_file.write_text(config, encoding="utf-8", newline="\n")
        Notebook(self.notebook_file)
        self.logs_dir.mkdir(exist_ok=True)

        self._ignore(*(path.name for path in (self.session_file, self.logs_dir, self.visualization_file)))

        # The ignore file a project brought along is not JRI's to
        # rewrite, so only a repository JRI creates gets one, and what
        # it holds is what keeps those patterns out of the first commit
        # the user makes.
        if repository_created and not self.project_gitignore_file.exists():
            self.project_gitignore_file.write_text(
                f"{'\n'.join(self.PROJECT_IGNORES)}\n", encoding="utf-8", newline="\n"
            )
        return Installation(self, created=created, repository_created=repository_created)

    # Read back and topped up on every call, since a rule checked for
    # its existence alone is one nothing puts back once something has
    # replaced it. The file is the one JRI commits, so Git holds what
    # it says and reports a line going missing -- where a rule sitting
    # in the directory it hides ignores itself, and takes its own
    # absence with it.
    def _ignore(self, *patterns: str) -> None:
        content = self.gitignore_file.read_text(encoding="utf-8") if self.gitignore_file.exists() else ""
        missing = [pattern for pattern in patterns if pattern not in content.splitlines()]
        if not missing:
            return
        separator = "" if not content or content.endswith("\n") else "\n"
        self.gitignore_file.write_text(f"{content}{separator}{'\n'.join(missing)}\n", encoding="utf-8", newline="\n")


@dataclass(frozen=True)
class Installation:
    workspace: Workspace
    created: bool
    repository_created: bool


class Hold:
    # The claim is held for the two system calls that take the lock and
    # name the taker, so one still standing after this is one nothing
    # here can wait out.
    CLAIMED_WITHIN = 1.0
    # A window that stopped answering still holds the project, so what
    # a takeover waits for is the operating system freeing the lock of
    # a process that is gone.
    FREED_WITHIN = 5.0
    POLL = 0.05

    def __init__(self, workspace: Workspace) -> None:
        self.lock = Lock(workspace.root / paths.LOCK_FILE)
        self.claim = Lock(workspace.root / paths.CLAIM_FILE)
        self.holder: int | None = None

    # Whether this process has the project now, and where it does not,
    # the pid of the JRI that has it. The lock is what says the holder
    # is running, since the operating system frees it when its holder
    # dies; the record inside it is what says which process that is,
    # and it was written under the claim this reads it under.
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

    # The other window is killed rather than asked to close, since one
    # that stopped answering would never hear the asking. What says it
    # ended is the lock coming free -- the operating system's answer
    # about the process -- and never the signal being sent.
    def evict(self) -> bool:
        holder = self.holder
        if holder is not None:
            logger.info("hold_eviction_started holder=%d", holder)
            try:
                # One process and never a group: what that window
                # started is in a session of its own, and the terminal
                # the user is sitting in is in this one.
                os.kill(holder, signal.SIGTERM)
            except OSError:
                # A window that ended while the question stood is a
                # window this has nothing left to end.
                logger.exception("hold_kill_failed holder=%d", holder)
        deadline = time.monotonic() + self.FREED_WITHIN
        while not self.take():
            if time.monotonic() >= deadline:
                logger.info("hold_eviction_failed holder=%d", holder)
                return False
            time.sleep(self.POLL)
        logger.info("hold_eviction_finished holder=%d", holder)
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
