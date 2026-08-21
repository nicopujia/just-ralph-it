import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from contextlib import closing, suppress
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
from .workspace import MAX_PID, Hold, Workspace

# Start the runner as `jri start` without a console script. `pip install --user` can omit that script from `PATH`.
RUNNER_COMMAND = ("-m", "jri", "start")
# This variable names the window that spawned the runner. A window spawns its runner while it holds the project.
# The runner needs a way to name the window that started it.
# A run that a user starts by hand would report to a conversation that did not ask for it.
HOLDER_VARIABLE = "JRI_HOLDER"
# A signal to 0 or to 1 reaches more than one run. `kill` reads 0 as the group of the caller.
# `killpg(1)` reaches every process that the user owns.
# A runner in a container writes the process it has in its own namespace, which is 1, in the lock file.
# The container and the host share that lock file. Report such a run, and refuse to signal it.
MIN_SIGNALLED_PID = 2

logger = logging.getLogger(__name__)


# The journal header identifies the JRI version and process. Report readers can identify the writer.
# Never read it as an instruction. The record schema defines the journal shape.
class Header(BaseModel):
    version: str
    pid: int
    started: AwareDatetime

    model_config = ConfigDict(extra="forbid")


# The model streams this reasoning under its row. The journal has no `TextDelta` record.
# Analyst JSON and explorer reports are answers to JRI. Only the interviewer sends turn replies.
# Another process reads this journal, so JRI keeps the refusal detail with the bytes that the run returns.
class Thought(BaseModel):
    kind: Literal["thought"]
    text: str

    model_config = ConfigDict(extra="forbid")


# This records a row and the start time of its call. Use the shared wall clock, and not a monotonic clock.
# A monotonic clock counts inside one process only. The time must have a zone.
# JRI cannot compare a naive time with the aware `now`, so it rejects such a time as a record it did not write.
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

    # This states whether the run could do the work that the user asked for.
    # A stopped run did what the user told it to do.
    # A run that found something to clarify did its work, and asks a question about that work.
    @property
    def failure(self) -> bool:
        return self.ending in {"exhausted", "refused", "unavailable", "blocked", "failed"}


class Record(RootModel[Thought | RowOpened | RowClosed | Conclusion]): ...


# This states the project state now: the window that holds the project, the run that is alive, and the record that
# no window removed yet. Each read fills every field, so no field has a default that could hide a missing value.
# `recorded` says that a journal is present. `ending` is the ending of a run that no window removed yet.
# `draft` is saved work that a start continues and a halt can lose.
class Status(BaseModel):
    holder: int | None
    pid: int | None
    started: AwareDatetime | None
    step: str
    step_started: AwareDatetime | None
    stopping: bool
    recorded: bool
    ending: str
    draft: bool

    model_config = ConfigDict(extra="forbid")


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
        # A run owns only these files.
        # The directory ignore rule and the draft belong to the workspace and to `Specs`.
        # They outlive a run.
        self.journal_file = workspace.root / paths.JOURNAL_FILE
        self.cancel_file = workspace.root / paths.CANCEL_FILE
        self.runner_log_file = workspace.root / paths.RUNNER_LOG_FILE
        self.lock = Lock(workspace.root / paths.GENERATION_LOCK_FILE)

    # This method defines the runner lifetime. The lock states that the runner is alive.
    # The journal states what the runner did, and the cancel file is its only input.
    # Answer with the conclusion that the run wrote. A run without a window reports through its caller.
    # JRI removes the journal before that caller can read it.
    @classmethod
    def execute(cls, settings: Settings, began: "Callable[[], None] | None" = None) -> Conclusion:
        generation = cls(Workspace.find())
        # Refuse before JRI writes a file, so a refused start does not change the project.
        # A window that holds the project spawns its own runner, and that runner names its window in the environment.
        # Read the hold directly, because JRI would make the workspace directory when it opens the hold.
        # This refusal must not make that directory.
        holder = Hold(generation.workspace).find_holder()
        if holder is not None and os.environ.get(HOLDER_VARIABLE) != str(holder):
            raise PersistenceError(
                f"A JRI window holds this project, in the window running process {holder}. It owns the conversation "
                "that a generation reports to, so nothing started. Start the generation from that window."
            )
        # Open the directory through the workspace.
        # The workspace writes its Git ignore rule before the lock file and the first journal line.
        generation.workspace.open_generation_dir()
        if not generation.lock.take(str(os.getpid())):
            raise PersistenceError("A generation is already running in this project.")
        # This process holds the lock, so no runner and no follower holds these files.
        # Remove the files that a run which already ended left. A stale cancel file would stop this run immediately.
        # Do not call `discard`, because it waits for the lock that this process holds.
        generation._remove_records()
        # The run holds its caller from here to its ending. Tell that caller that the run began.
        # Tell it after every possible refusal, and never before one.
        if began is not None:
            began()
        stopping = threading.Event()
        cancelled = threading.Event()
        watcher = threading.Thread(
            target=_watch_cancellation, args=(generation.cancel_file, cancelled, stopping), daemon=True
        )
        try:
            watcher.start()
            return _write_journal(generation.journal_file, cls.record(settings, cancelled))
        finally:
            stopping.set()
            generation.lock.release()

    # This states whether a run left a record that no window removed yet.
    # A live run and a run without a watcher write the same state to disk.
    # Follow both runs, because only the follower can identify which run it reads.
    @property
    def exists(self) -> bool:
        return self.journal_file.exists()

    # This states whether a runner is alive, even before it writes a journal line.
    # The operating system releases the lock of a runner when that runner exits.
    # Check first that the lock file exists.
    # JRI would make a generation directory when it takes a lock that is not there.
    # An unreadable lock does not prove that no runner is alive.
    # Report the unreadable directory as project state, and not as a window traceback.
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

    # Record the workflow result in the journal. Classify a failure only here.
    # The process that reads the journal then gets the same turn ending from the exception class.
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

    def spawn(self) -> None:
        self.workspace.open_generation_dir()
        try:
            # A held lock means that a run is active. Do not remove its files, and do not start a second run.
            if self.lock.is_held():
                raise PersistenceError("A generation is already running in this project.")
            # Remove every file that a run which already ended left, the cancel file included.
            # A stale cancel file would stop the new run before it starts.
            self.discard()
            # The runner writes its log through `logs.configure`.
            # This file gets only the output that escapes that log: import failures, library standard error,
            # and tracebacks.
            with self.runner_log_file.open("wb") as errors:
                process = subprocess.Popen(
                    [sys.executable, *RUNNER_COMMAND],
                    cwd=self.workspace.root,
                    # This window holds the project. Name it, so its runner knows which conversation asked for it.
                    env={**os.environ, HOLDER_VARIABLE: str(os.getpid())},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
                    **_detach(),
                )
        # An unwritable run directory is project state, and not a runner crash.
        # No run started, so JRI must remove nothing.
        except (LockError, OSError) as error:
            logger.exception("generation_spawn_failed root=%r", self.workspace.root)
            raise PersistenceError(
                f"Could not start the generation in `{self.workspace.generation_dir}`: {error}"
            ) from error
        logger.info("generation_spawned pid=%d", process.pid)
        # Wait for the child, and do not assume that it started.
        # A window would otherwise report a failed runner as an active one.
        # The first poll of a missing journal would look like a process that already exited.
        deadline = time.monotonic() + self.STARTS_WITHIN
        while not self.exists:
            if process.poll() is not None:
                raise Error(f"JRI could not start the generation. {self._read_errors()}")
            if time.monotonic() >= deadline:
                raise Error("JRI could not start the generation: it never wrote anything down.")
            time.sleep(self.POLL)

    # Ask the run to stop, and change nothing when no run is alive.
    # The runner reads this file and writes its own ending, so the turn still closes.
    def stop(self) -> bool:
        if not self.is_running:
            return False
        self.cancel_file.touch()
        return True

    # End the run now. A halt must look exactly like a machine that loses power, so remove no file.
    # JRI already recovers from a dead run, and it can lose the work of the current iteration.
    # A free lock proves that the operating system ended the process. A sent signal does not prove it.
    def halt(self) -> bool:
        if not self.is_running:
            return False
        pid = self._read_pid()
        # Signal only a process that JRI recorded. Do not guess at a process from a record JRI did not write.
        if pid is None:
            raise PersistenceError(
                f"Something holds `{self.lock.path}` without saying what it is, so JRI will not end it. "
                "Find the process that holds it and end it yourself, then try again."
            )
        # A run that this process cannot signal is still a run. Report why JRI stops, and signal no other process.
        if pid < MIN_SIGNALLED_PID:
            raise PersistenceError(
                f"The generation records process {pid}, which names no single process to end. A run inside a "
                "container records the process it wears in its own namespace. Stop that container instead."
            )
        _kill(pid)
        deadline = time.monotonic() + self.FREED_WITHIN
        while self.lock.is_held():
            if time.monotonic() >= deadline:
                raise PersistenceError(
                    f"JRI ended the generation process {pid}, and it still holds `{self.lock.path}`. "
                    "Wait a moment, then try again."
                )
            time.sleep(self.POLL)
        return True

    # This reads the state of the project. It makes no file and it removes no file.
    # A project with no run stays a project with no run. Read each file only after JRI finds it.
    def read_status(self) -> Status:
        running = self.is_running
        header: Header | None = None
        conclusion: Conclusion | None = None
        # Keep each row that opened until its close arrives. The last row that stays open is the current step.
        opened: dict[str, RowOpened] = {}
        for record in self._read_records():
            match record:
                case Header():
                    header = record
                case RowOpened():
                    opened[record.call_id] = record
                case RowClosed():
                    opened.pop(record.call_id, None)
                case Conclusion():
                    conclusion = record
        step = next(reversed(opened.values()), None)
        return Status(
            holder=Hold(self.workspace).find_holder(),
            pid=self._read_pid() if running else None,
            started=header.started if running and header is not None else None,
            step=step.label if step is not None else "",
            step_started=step.started if step is not None else None,
            stopping=running and self.cancel_file.exists(),
            recorded=self.exists,
            ending=conclusion.ending if conclusion is not None else "",
            draft=self.workspace.draft_file.exists(),
        )

    # Yield every run record in journal order, from its first line, even when JRI starts to watch late.
    # A stop request writes the cancel file. Only the other process can end itself, after it reads that file.
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
        # The runner released the lock and wrote no ending. Its process exited and cannot finish the run.
        raise Error("The generation stopped before it finished, and its process is gone. Try again.")

    # Remove only the files that this run wrote, and remove them by name.
    # Do not remove the lock, `.gitignore`, or `draft.patch`.
    # A free lock and the project hold prove that no runner and no second follower uses these files.
    # Wait after the ending, because the runner can write that ending before it exits.
    # A new run would otherwise continue this record.
    # Keep the record while the lock stays held.
    # The directory cannot show whether a dead window left that record.
    # Only a runner slower than this wait causes that case, such as a stopped process or a filesystem that hangs.
    def discard(self) -> None:
        deadline = time.monotonic() + self.FREED_WITHIN
        while self.lock.is_held():
            if time.monotonic() >= deadline:
                logger.info("generation_record_kept reason=still_locked")
                return
            time.sleep(self.POLL)
        self._remove_records()

    # Remove the files by name, and only after the caller finds that no other process can hold them.
    def _remove_records(self) -> None:
        for path in (self.journal_file, self.cancel_file, self.runner_log_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.exception("generation_record_removal_failed path=%r", path)
        self._drop_worktrees()

    # A killed run leaves the directories it worked in. A run that ends removes its own directories.
    # Every directory that is here now belongs to a run that cannot use it again.
    def _drop_worktrees(self) -> None:
        for directory in (paths.WORKTREE_DIR, paths.SNAPSHOT_DIR, paths.PRE_IMAGE_DIR):
            files.remove_directory(self.workspace.root / directory)

    # Yield each run record, and its ending when the journal has one.
    # Close the reader before this method returns and before it fails.
    # A suspended reader keeps the journal open. Windows cannot remove a file that this process still holds open.
    def _watch(
        self, cancelled: threading.Event | None, detached: threading.Event | None
    ) -> Generator["specs_generation.Progress", None, Conclusion | None]:
        requested = False
        thought = ""
        with closing(self._read()) as records:
            for record in records:
                # Send the requested stop before JRI checks whether the window left.
                # A user who closes the window immediately after a request still stops the run.
                if cancelled is not None and cancelled.is_set() and not requested:
                    self.cancel_file.touch()
                    requested = True
                    logger.info("generation_stop_requested")
                # The window is gone, but it does not own the run. Remove no file.
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
        # The reader yields `None` before it ends. The loop above then flushes every pending thought.
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
                # A pass with no record is still an update. A model call can write no record for minutes.
                # The reader could otherwise not process a stop until the call ends.
                yield None
                # Read one more time before this reader ends.
                # The runner writes its ending before it releases the lock.
                # The file can hold an ending when the runner released the lock after the prior read.
                last = not self.lock.is_held()
                if not last:
                    time.sleep(self.POLL)

    # Read the process that the run recorded when it took its lock.
    # Return nothing for a record that JRI did not write.
    def _read_pid(self) -> int | None:
        record = self.lock.holder
        return int(record) if record.isdigit() and int(record) <= MAX_PID else None

    # Yield every complete record that the journal holds now, the header included.
    # Ignore a line that JRI cannot read. A killed append leaves a partial last line, and a report shows only
    # the records that JRI can read.
    def _read_records(self) -> Generator[Header | Thought | RowOpened | RowClosed | Conclusion]:
        if not self.exists:
            return
        try:
            lines = self.journal_file.read_bytes().split(b"\n")
        # Another window can remove this journal while this read runs. A journal that is gone reports nothing.
        except OSError:
            logger.exception("generation_journal_unreadable path=%r", self.journal_file)
            return
        for number, line in enumerate(lines):
            # A journal ends with a newline, so the split leaves an empty last line. That line is not a record.
            if not line:
                continue
            try:
                yield Header.model_validate_json(line) if not number else Record.model_validate_json(line).root
            except ValidationError:
                continue

    def _read_errors(self) -> str:
        try:
            errors = self.runner_log_file.read_bytes()[-self.REPORTED_ERROR_BYTES :]
        except OSError:
            return "It wrote nothing about why."
        return errors.decode(errors="replace").strip() or "It wrote nothing about why."


# Rebuild the workflow result from the recorded ending. Return every failure as its original workflow exception class.
# JRI then gets the turn ending in its normal place.
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
    # Flush every record so a reader sees it. Sync only the opening and the ending, which a crash must not lose.
    # A sync of 12,000 records took 6.6 s instead of 0.02 s.
    # A reader accepts a partial last line, and reads a lost end of the file as an interruption.
    journal.flush()
    if sync:
        os.fsync(journal.fileno())


# Tell `Popen` how to detach the runner on each platform. CPython ignores `start_new_session` on Windows.
# Only the Windows flags can put the runner outside the console of the window.
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


# Kill the process group of a runner that leads a group. Kill only the process of a runner that leads no group.
# A runner that a window spawned starts its own session (`start_new_session`), so its process group ID is its pid.
# That group holds the Git and provider processes that the runner started.
# A runner that a user starts by hand shares the group of its terminal.
# That group holds processes that are not JRI.
# A process that already ended needs no signal, and the lock check after this call gives the answer.
def _kill(pid: int) -> None:
    if sys.platform != "win32":
        with suppress(OSError):
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        return
    # Windows cannot signal a process group. `taskkill` ends this process and every process below it.
    executable = shutil.which("taskkill")
    if executable is not None:
        subprocess.run([executable, "/F", "/T", "/PID", str(pid)], check=False, capture_output=True)


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
# The live recording combines them in the same way.
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


# Write the complete record of one run. The header proves that the run started.
# The conclusion proves that the run ended. A journal without a conclusion belongs to a dead process.
# Read the events one at a time, because a `for` loop discards the return value of a generator.
# Truncate the journal while this process holds the lock. No other run writes it, and nobody reads a journal
# that a window already removed.
def _write_journal(path: Path, events: Generator["specs_generation.Progress", None, Conclusion]) -> Conclusion:
    logger.info("generation_started pid=%d", os.getpid())
    with path.open("wb") as journal:
        _append(journal, Header(version=__version__, pid=os.getpid(), started=datetime.now(UTC)), sync=True)
        batch = ""
        written = time.monotonic()
        while True:
            try:
                event = next(events)
            except StopIteration as ending:
                conclusion = cast("Conclusion", ending.value)
                break
            record = _describe(event)
            # A model streams its reasoning one word at a time. Hold those deltas for the poll interval of the reader.
            # The reader joins every thought that one pass finds.
            # A batch gives the reader the same text in the same order, in one line instead of hundreds.
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
    return conclusion


# Send a stop to the run through a file, not a signal. The runner has its own process group on every platform.
# Windows has no signal that can reach that process group.
def _watch_cancellation(cancel_file: Path, cancelled: threading.Event, stopping: threading.Event) -> None:
    while not stopping.wait(Generation.POLL / 2):
        if cancel_file.exists():
            logger.info("generation_stop_read")
            cancelled.set()
            return
