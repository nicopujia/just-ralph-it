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

# A PID is a 32-bit number on both platforms. A larger value identifies no process and is not a JRI record.
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
    def settings_file(self) -> Path:
        return self.root / paths.SETTINGS_FILE

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
        # Use rooted, explicit names.
        # These rules apply only to these files, not same-named files in a specification tree.
        self._ignore(*(f"/{Path(path).name}" for path in (paths.LOCK_FILE, paths.CLAIM_FILE)))
        return Hold(self)

    # This directory holds run data while it works. It never holds committed data.
    def open_generation_dir(self) -> Path:
        self.generation_dir.mkdir(exist_ok=True, parents=True)
        # Root this rule at the workspace and end it with a slash.
        # It matches this directory, not `generation` in a specification tree.
        self._ignore(f"/{self.generation_dir.name}/")
        return self.generation_dir

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
        Repository.init(self.root)
        created = not self.settings_file.exists()
        if reset is not None:
            self._clear()
        self.directory.mkdir(exist_ok=True, parents=True)
        if created or reset is not None:
            self.settings_file.write_text(settings, encoding="utf-8", newline="\n")
        Notebook(self.notebook_file)
        self.logs_dir.mkdir(exist_ok=True)

        self._ignore(*(path.name for path in (self.session_file, self.logs_dir, self.visualization_file)))

        # A project-provided ignore file is not JRI-owned. Create one only with a new repository.
        # Its patterns stay out of the user's first commit.
        if repository_created and not self.project_gitignore_file.exists():
            self.project_gitignore_file.write_text(
                f"{'\n'.join(self.PROJECT_IGNORES)}\n", encoding="utf-8", newline="\n"
            )
        return Installation(self, created=created, repository_created=repository_created)

    # These are all paths that `--force` replaces, whether they exist or not. The workspace owns this list.
    @property
    def _reset_paths(self) -> tuple[Path, ...]:
        return tuple(self.root / path for path in paths.RESET_PATHS)

    def _clear(self) -> None:
        for path in self._reset_paths:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)

    # Read and add rules on every call. An existing file can lose a rule after replacement.
    # JRI commits this file, so Git reports a missing rule.
    # A rule inside the hidden directory would hide its own absence.
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
    POLL = 0.05

    def __init__(self, workspace: Workspace) -> None:
        self.lock = Lock(workspace.root / paths.LOCK_FILE)
        self.claim = Lock(workspace.root / paths.CLAIM_FILE)
        self.holder: int | None = None

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
        deadline = time.monotonic() + self.FREED_WITHIN
        # Signal the PID read under the current claim, not a PID read before the user chose eviction.
        # A PID can be reused when its process exits. Signal only the process holding the project now.
        while not self.take():
            holder = self.holder
            if signalled is None and holder is not None:
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
