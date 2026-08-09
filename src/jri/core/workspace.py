import logging
import os
import shutil
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from jri.lib import git
from jri.lib.lock import Lock

from . import paths
from .exceptions import PersistenceError
from .notes import Notebook
from .repository import Repository

if TYPE_CHECKING:
    from collections.abc import Generator

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

    # A reset that nothing is standing in the way of, and the
    # permission to go through with one. Both refusals are read before
    # the caller is handed anything, so a command that warns about what
    # `--force` deletes warns only where JRI would go through with it,
    # rather than asking for an answer it then has nothing to do with.
    #
    # A reset empties what two live processes are writing, and neither
    # answers for the other: the window that has the project writes the
    # notes, the conversation and the logs, and a run in a process of
    # its own writes the run directory, which outlives the window that
    # started it. So the project is taken the way a chat takes it, and
    # the run is asked about through the lock it holds for as long as it
    # lives. The run is the worse of the two to lose: a runner whose
    # lock went with the directory leaves the next Ralph reading no run
    # in flight and starting a second one beside it, each on an inode
    # the other cannot see.
    #
    # The project is held for as long as the caller keeps this, which
    # is what makes asking late an answer about now rather than about
    # then: a second window is refused the project meanwhile, and a run
    # is only ever started by a window that has the project, so neither
    # answer can turn from no to yes under a question. A run that ends
    # while one is on screen only leaves less to lose than was said.
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
            # A lock file that is not there is a lock nothing holds, and
            # taking one to ask with would put the run directory back a
            # moment before this empties it.
            if generation_lock.exists() and Lock(generation_lock).is_held():
                raise PersistenceError(
                    "A Just Ralph It run is still going in this project, in a process of its own. A reset takes "
                    "the run directory away from it, and the run after that would start beside it rather than "
                    "after it, so nothing was deleted. Run `jri chat` to watch it or stop it, then try again."
                )
            yield Reset(tuple(path for path in (self.config_file, *self._reset_paths) if path.exists()))
        finally:
            hold.release()

    # The rendered configuration comes in rather than being read from
    # `Settings`, so locating a workspace never depends on loading one.
    # Starting over takes a `Reset`, which only `open_reset` hands out
    # and only under the hold: what empties a project is never a flag a
    # caller can set without having asked the project about it first.
    def install(self, config: str, *, reset: "Reset | None" = None) -> "Installation":
        repository_created = git.find_root(self.root) is None
        Repository.init(self.root)
        created = not self.config_file.exists()
        if reset is not None:
            self._clear()
        self.directory.mkdir(exist_ok=True, parents=True)
        if created or reset is not None:
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

    # Everything `--force` replaces, whether it is there or not, which
    # is the workspace's own list rather than a caller's.
    @property
    def _reset_paths(self) -> tuple[Path, ...]:
        return tuple(self.root / path for path in paths.RESET_PATHS)

    def _clear(self) -> None:
        for path in self._reset_paths:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)

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


# A reset JRI would go through with, and what it replaces as that
# stood when the refusals were read. Only `Workspace.open_reset` hands
# one out, and only for as long as it holds the project, so a caller
# holding one has both refusals behind it and the state they answered
# about still standing.
@dataclass(frozen=True)
class Reset:
    paths: tuple[Path, ...]


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
        signalled: int | None = None
        deadline = time.monotonic() + self.FREED_WITHIN
        # What is signalled is the pid the refusal in the condition
        # above just read under the claim, and never the one the `take`
        # before the question read: the answer took however long the
        # user took, a pid is handed on the moment its process ends, and
        # the number a window that let the project go leaves behind
        # belongs to whoever wears it next. So there is one process this
        # can end, and it is the one holding the project this instant.
        while not self.take():
            holder = self.holder
            if signalled is None and holder is not None:
                logger.info("hold_eviction_started holder=%d", holder)
                try:
                    # One process and never a group: what that window
                    # started is in a session of its own, and the
                    # terminal the user is sitting in is in this one.
                    os.kill(holder, signal.SIGTERM)
                except OSError:
                    # A window that ended between that read and this is
                    # a window this has nothing left to end.
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
