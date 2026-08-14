import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, Literal, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, RootModel, ValidationError

from jri import __version__
from jri.lib import files
from jri.lib.lock import Lock, LockError

from . import paths
from .ai import Outcome, ReasoningDelta, ToolCallFinished, ToolCallStarted, specs_generation
from .exceptions import (
    Error,
    PersistenceError,
    ProviderRefusalError,
    ProviderUnavailableError,
    RepositoryStateError,
    RunDetached,
    UsageLimitError,
)
from .settings import Settings
from .workspace import Workspace

# Start the runner as `jri generate` without a console script. `pip install --user` can omit that script from `PATH`.
RUNNER_COMMAND = ("-m", "jri", "generate")

logger = logging.getLogger(__name__)


# The journal header identifies the JRI version and process. Report readers can identify the writer.
# Never read it as an instruction. The record schema defines the journal shape.
class Header(BaseModel):
    version: str
    pid: int
    started: str

    model_config = ConfigDict(extra="forbid")


# This is model reasoning streamed under its row. The journal has no `TextDelta` record.
# Analyst JSON and explorer reports are answers to JRI. Only the interviewer sends turn replies.
# Another process parses this journal, so refusal details must persist with the returned bytes.
class Thought(BaseModel):
    kind: Literal["thought"]
    text: str

    model_config = ConfigDict(extra="forbid")


# This records a row and the start time of its call. Use the shared wall clock, not a process-local monotonic clock.
# The time must have a zone.
# A naive time cannot be compared with aware `now`, so reject it as a record JRI did not write.
class RowOpened(BaseModel):
    kind: Literal["row_opened"]
    call_id: str
    label: str
    symbol: str
    depth: int
    started: AwareDatetime

    model_config = ConfigDict(extra="forbid")


class RowClosed(BaseModel):
    kind: Literal["row_closed"]
    call_id: str
    label: str
    outcome: Outcome
    detail: str
    depth: int

    model_config = ConfigDict(extra="forbid")


# This is the last journal line and the only record of the run ending.
# Each ending names the workflow result.
# The reader rebuilds that result instead of trusting a `commit` on a non-commit ending.
class Conclusion(BaseModel):
    kind: Literal["conclusion"]
    ending: Literal[
        "committed", "unchanged", "ambiguities", "stopped", "exhausted", "refused", "unavailable", "blocked", "failed"
    ]
    commit: str = ""
    ambiguities: tuple[str, ...] = ()
    detail: str = ""

    model_config = ConfigDict(extra="forbid")


class Record(RootModel[Thought | RowOpened | RowClosed | Conclusion]): ...


# This generation runs in its own process. The requesting window does not keep it alive.
# The processes share only the run directory. The runner appends the journal and never opens the session.
# A watcher reads the journal and never writes it.
class Generation:
    POLL = 0.1
    # Give a runner this long to exit and release its lock after it writes its ending.
    FREED_WITHIN = 5.0
    # A runner imports JRI before it writes. A cold or busy machine can delay this step.
    # This limit detects a child that never starts.
    STARTS_WITHIN = 60.0
    # Keep enough runner error output to identify a crash in a message that fits on a screen.
    REPORTED_ERROR_BYTES = 2000

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        # These are the only files owned by a run.
        # The directory ignore rule and draft belong to the workspace and `Specs`.
        # They outlive a run.
        self.journal_file = workspace.root / paths.JOURNAL_FILE
        self.cancel_file = workspace.root / paths.CANCEL_FILE
        self.runner_log_file = workspace.root / paths.RUNNER_LOG_FILE
        self.lock = Lock(workspace.root / paths.GENERATION_LOCK_FILE)

    # This method defines the runner lifetime. The lock states that it is alive, the journal states what it did,
    # and the cancel file is its only input.
    @classmethod
    def execute(cls, settings: Settings) -> None:
        generation = cls(Workspace.find())
        # Open the directory through the workspace.
        # Its Git ignore rule exists before the lock file and first journal line.
        generation.workspace.open_generation_dir()
        if not generation.lock.take(str(os.getpid())):
            raise PersistenceError("A generation is already running in this project.")
        stopping = threading.Event()
        cancelled = threading.Event()
        watcher = threading.Thread(
            target=_watch_cancellation, args=(generation.cancel_file, cancelled, stopping), daemon=True
        )
        try:
            watcher.start()
            _write_journal(generation.journal_file, cls.record(settings, cancelled))
        finally:
            stopping.set()
            generation.lock.release()

    # This states whether a run has an unfolded record. A live run and a run without a watcher have the same disk state.
    # Follow both runs. The follower distinguishes them.
    @property
    def exists(self) -> bool:
        return self.journal_file.exists()

    # This states whether a runner is alive, even before it writes a journal line.
    # The operating system releases its lock at exit.
    # Check that the lock file exists first.
    # Taking a missing lock would create a generation directory while checking it.
    # An unreadable lock does not mean no runner exists.
    # Report the unreadable directory as project state, not a window traceback.
    @property
    def is_running(self) -> bool:
        if not self.lock.path.exists():
            return False
        try:
            return self.lock.is_held()
        except LockError as error:
            logger.exception("generation_lock_unreadable path=%r", self.lock.path)
            raise PersistenceError(
                f"Could not read the generation in `{self.workspace.generation_dir}`: {error}"
            ) from error

    # Record the workflow result in the journal. Classify failures only here.
    # The process that folds the journal then derives the same turn ending from the exception class.
    @staticmethod
    def record(
        settings: Settings, cancelled: threading.Event
    ) -> Generator["specs_generation.Progress", None, Conclusion]:
        try:
            result = yield from specs_generation.generate(settings, cancelled)
        except UsageLimitError as error:
            logger.exception("generation_exhausted")
            return Conclusion(kind="conclusion", ending="exhausted", detail=str(error))
        except ProviderRefusalError as error:
            logger.exception("generation_refused")
            return Conclusion(kind="conclusion", ending="refused", detail=str(error))
        except ProviderUnavailableError as error:
            logger.exception("generation_unavailable")
            return Conclusion(kind="conclusion", ending="unavailable", detail=str(error))
        except RepositoryStateError as error:
            logger.info("generation_blocked reason=%s", error)
            return Conclusion(kind="conclusion", ending="blocked", detail=str(error))
        except Exception as error:
            logger.exception("generation_failed")
            return Conclusion(kind="conclusion", ending="failed", detail=str(error))
        match result:
            case None:
                return Conclusion(kind="conclusion", ending="stopped")
            case str():
                return Conclusion(kind="conclusion", ending="committed", commit=result)
            case specs_generation.Unchanged():
                return Conclusion(kind="conclusion", ending="unchanged")
            case specs_generation.Ambiguities():
                return Conclusion(kind="conclusion", ending="ambiguities", ambiguities=tuple(result.ambiguities))

    def start(self) -> None:
        self.workspace.open_generation_dir()
        try:
            # A held lock means that a run is active. Do not remove its files or start another run beside it.
            if self.lock.is_held():
                raise PersistenceError("A generation is already running in this project.")
            # Remove every file left by an already folded run, including the cancel file.
            # A stale cancel file would stop the new run before it starts.
            self.discard()
            # The runner writes its log through `logs.configure`. This file receives only escaped output:
            # import failures, library standard error, and tracebacks.
            with self.runner_log_file.open("wb") as errors:
                process = subprocess.Popen(
                    [sys.executable, *RUNNER_COMMAND],
                    cwd=self.workspace.root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
                    **_detach(),
                )
        # An unwritable run directory is project state, not a runner crash. No run started, so nothing requires cleanup.
        except (LockError, OSError) as error:
            logger.exception("generation_spawn_failed root=%r", self.workspace.root)
            raise PersistenceError(
                f"Could not start the generation in `{self.workspace.generation_dir}`: {error}"
            ) from error
        logger.info("generation_spawned pid=%d", process.pid)
        # Wait for the child instead of assuming that it started. Otherwise, report a failed runner as active.
        # The first poll of a missing journal would look like a process that already exited.
        deadline = time.monotonic() + self.STARTS_WITHIN
        while not self.exists:
            if process.poll() is not None:
                raise Error(f"JRI could not start the generation. {self._read_errors()}")
            if time.monotonic() >= deadline:
                raise Error("JRI could not start the generation: it never wrote anything down.")
            time.sleep(self.POLL)

    # Yield every run record in journal order, from its first line, even when watching starts late.
    # Stop requests write the cancel file. Only the other process can end itself after it reads this file.
    def follow(
        self, cancelled: threading.Event | None = None, detached: threading.Event | None = None
    ) -> Generator["specs_generation.Progress", None, "specs_generation.SpecsResult | None"]:
        try:
            conclusion = yield from self._watch(cancelled, detached)
        # An unreadable record ends a run like a failed record.
        # Remove its journal so later runs do not meet the same failure.
        except Error:
            self.discard()
            raise
        self.discard()
        if conclusion is not None:
            return _answer(conclusion)
        # The lock was released without an ending. Its process exited and cannot finish the run.
        raise Error("The generation stopped before it finished, and its process is gone. Try again.")

    # Remove only files written by this run, and remove them by name.
    # Do not remove the lock, `.gitignore`, or `draft.patch`.
    # A free lock and the project hold prove no runner or second follower uses these files.
    # Wait after the ending because the runner can write it before exit.
    # A new run would otherwise attach to this record.
    # Keep the record when the lock remains held.
    # The directory cannot distinguish it from a record left by a dead window.
    # This case requires a runner slower than the post-write wait, such as a stopped process or hung file system.
    def discard(self) -> None:
        deadline = time.monotonic() + self.FREED_WITHIN
        while self.lock.is_held():
            if time.monotonic() >= deadline:
                logger.info("generation_record_kept reason=still_locked")
                return
            time.sleep(self.POLL)
        for path in (self.journal_file, self.cancel_file, self.runner_log_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception("generation_record_removal_failed path=%r", path)
        self._drop_worktrees()

    # A killed run leaves the directories it worked in. A run that ends removes its own, so every directory that
    # stands here now was left by a run that cannot come back for it.
    def _drop_worktrees(self) -> None:
        for directory in (paths.WORKTREE_DIR, paths.SNAPSHOT_DIR, paths.PRE_IMAGE_DIR):
            files.remove_directory(self.workspace.root / directory)

    # Yield each run record and its ending when present. Close the reader before return or failure.
    # A suspended reader keeps the journal open. Windows cannot remove a file that this process still has open.
    def _watch(
        self, cancelled: threading.Event | None, detached: threading.Event | None
    ) -> Generator["specs_generation.Progress", None, Conclusion | None]:
        requested = False
        thought = ""
        with closing(self._read()) as records:
            for record in records:
                # Send the requested stop before checking whether the window left.
                # Closing the window immediately after a request still stops the run.
                if cancelled is not None and cancelled.is_set() and not requested:
                    self.cancel_file.touch()
                    requested = True
                    logger.info("generation_stop_requested")
                # The window is gone, but it does not own the run. Do not fold or unlink anything.
                # The journal is the record, and the next window reads it from the start.
                if detached is not None and detached.is_set():
                    logger.info("generation_detached")
                    raise RunDetached
                if isinstance(record, Thought):
                    thought += record.text
                    continue
                if thought:
                    yield ReasoningDelta(thought)
                    thought = ""
                if isinstance(record, Conclusion):
                    return record
                if record is not None:
                    yield _replay(record)
        # The reader yields `None` before it ends. The loop above therefore flushes every pending thought.
        return None

    # Yield each new journal line and `None` for every pass with no new line. Read only complete lines.
    # A killed append leaves a partial line without its newline.
    def _read(self) -> Generator[Thought | RowOpened | RowClosed | Conclusion | None]:
        pending = b""
        number = 0
        last = False
        with self.journal_file.open("rb") as journal:
            while True:
                lines = (pending + journal.read()).split(b"\n")
                pending = lines.pop()
                for line in lines:
                    yield _read_line(line, number)
                    number += 1
                if last:
                    return
                # A pass with no record is still an update. A model call can produce no records for minutes.
                # Otherwise, the reader cannot process a stop until the call ends.
                yield None
                # Read once more before ending. The runner writes its ending before it releases the lock.
                # A lock released after the prior read can have an ending in the file.
                last = not self.lock.is_held()
                if not last:
                    time.sleep(self.POLL)

    def _read_errors(self) -> str:
        try:
            errors = self.runner_log_file.read_bytes()[-self.REPORTED_ERROR_BYTES :]
        except OSError:
            return "It wrote nothing about why."
        return errors.decode(errors="replace").strip() or "It wrote nothing about why."


# Rebuild the workflow result from the recorded ending. Return every failure as its original workflow exception class.
# The turn ending is then derived in its normal place.
def _answer(conclusion: Conclusion) -> "specs_generation.SpecsResult | None":
    match conclusion.ending:
        case "committed":
            return conclusion.commit
        case "unchanged":
            return specs_generation.Unchanged()
        case "ambiguities":
            return specs_generation.Ambiguities(list(conclusion.ambiguities))
        case "stopped":
            return None
        case "exhausted":
            raise UsageLimitError(conclusion.detail)
        case "refused":
            raise ProviderRefusalError(conclusion.detail)
        case "unavailable":
            raise ProviderUnavailableError(conclusion.detail)
        case "blocked":
            raise RepositoryStateError(conclusion.detail)
        case "failed":
            raise Error(conclusion.detail)


def _append(journal: IO[bytes], record: BaseModel, *, sync: bool = False) -> None:
    journal.write(record.model_dump_json().encode() + b"\n")
    # Flush every record so a reader sees it. Sync only the opening and ending, which a crash must not lose.
    # Syncing 12,000 records took 6.6 s instead of 0.02 s.
    # Readers tolerate a partial last line and treat a lost tail as interruption.
    journal.flush()
    if sync:
        os.fsync(journal.fileno())


# Configure Popen detachment for each platform. CPython ignores `start_new_session` on Windows.
# Windows flags are the only way to place the runner outside the window console.
def _detach() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _describe(event: "specs_generation.Progress") -> Thought | RowOpened | RowClosed:
    match event:
        case ReasoningDelta():
            return Thought(kind="thought", text=event.text)
        case ToolCallStarted():
            return RowOpened(
                kind="row_opened",
                call_id=event.call_id,
                label=event.label,
                symbol=event.symbol,
                depth=event.depth,
                started=datetime.now(UTC) - timedelta(seconds=event.age),
            )
        case ToolCallFinished():
            return RowClosed(
                kind="row_closed",
                call_id=event.call_id,
                label=event.label,
                outcome=event.outcome,
                detail=event.detail,
                depth=event.depth,
            )


def _read_line(line: bytes, number: int) -> Thought | RowOpened | RowClosed | Conclusion | None:
    try:
        if not number:
            header = Header.model_validate_json(line)
            logger.info("generation_journal_opened version=%s pid=%d", header.version, header.pid)
            return None
        return Record.model_validate_json(line).root
    # A line that JRI cannot read is not a line that JRI wrote. Nobody can report a run with an unreadable record.
    except ValidationError as error:
        logger.info("generation_record_unreadable line=%d", number)
        raise Error("JRI could not read what this generation wrote down. Try again.") from error


# A thought never reaches this function. The journal reader combines consecutive thoughts into one delta.
# The live recording uses the same combination.
def _replay(record: RowOpened | RowClosed) -> "specs_generation.Progress":
    match record:
        case RowOpened():
            return ToolCallStarted(
                record.call_id,
                record.label,
                record.symbol,
                depth=record.depth,
                # A clock change can place the call start after this reading. A call with a future start has age zero.
                age=max((datetime.now(UTC) - record.started).total_seconds(), 0.0),
            )
        case RowClosed():
            return ToolCallFinished(
                record.call_id, record.label, record.outcome, detail=record.detail, depth=record.depth
            )


# Write the reasoning deltas that the run holds as one record. Return the empty batch that follows it.
def _write_thought(journal: IO[bytes], batch: str) -> str:
    if batch:
        _append(journal, Thought(kind="thought", text=batch))
    return ""


# Write the complete record of one run. The header proves that it started, and the conclusion proves that it ended.
# A journal without a conclusion has a dead process.
# Pull events one at a time because `for` discards a generator return.
# Truncate the journal under this process lock. No other run is writing, and a folded journal is never read again.
def _write_journal(path: Path, events: Generator["specs_generation.Progress", None, Conclusion]) -> None:
    logger.info("generation_started pid=%d", os.getpid())
    with path.open("wb") as journal:
        _append(journal, Header(version=__version__, pid=os.getpid(), started=datetime.now(UTC).isoformat()), sync=True)
        batch = ""
        written = time.monotonic()
        while True:
            try:
                event = next(events)
            except StopIteration as ending:
                conclusion = cast("Conclusion", ending.value)
                break
            record = _describe(event)
            # A model streams its reasoning one word at a time. Hold those deltas for the time the reader polls with.
            # The reader joins every thought that one pass finds, so a batch gives it the same text in the same order,
            # in one line instead of hundreds.
            if isinstance(record, Thought):
                batch += record.text
                if time.monotonic() - written >= Generation.POLL:
                    written = time.monotonic()
                    batch = _write_thought(journal, batch)
                continue
            # A row states what the reasoning before it belongs to. Write the batch before the row that follows it.
            batch = _write_thought(journal, batch)
            _append(journal, record)
        _write_thought(journal, batch)
        _append(journal, conclusion, sync=True)
    logger.info("generation_finished ending=%s", conclusion.ending)


# Send a stop to the run through a file, not a signal. The runner has its own process group on every platform.
# Windows has no signal that can reach that process group.
def _watch_cancellation(cancel_file: Path, cancelled: threading.Event, stopping: threading.Event) -> None:
    while not stopping.wait(Generation.POLL / 2):
        if cancel_file.exists():
            logger.info("generation_stop_read")
            cancelled.set()
            return
