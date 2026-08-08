import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, RootModel, ValidationError

from jri import __version__
from jri.lib.lock import Lock, LockError

from . import paths
from .ai import ReasoningDelta, ToolCallFinished, ToolCallStarted, specs_generation
from .exceptions import Error, PersistenceError, RepositoryStateError, RunDetached, UsageLimitError
from .settings import Settings
from .workspace import Workspace

# What the runner is started as, which is `jri generate` reached
# without depending on a console script a `pip install --user` may have
# left off the path.
RUNNER_COMMAND = ("-m", "jri", "generate")

logger = logging.getLogger(__name__)


# What the journal opens with, so that a reader knows which JRI wrote
# it and a human reading a report knows which process to look for. It
# is never read back as an instruction: what holds the records to a
# shape is the shape itself, below.
class Header(BaseModel):
    version: str
    pid: int
    started: str

    model_config = ConfigDict(extra="forbid")


# A model's own thinking, streamed under the row it belongs to. There
# is no record for a `TextDelta` and there is no room for one: the
# analyst's raw JSON and the explorer's report body are answers to JRI,
# and the one voice a turn renders as a reply is the interviewer's. A
# journal is a file on the user's disk that a second process parses, so
# the refusal has to hold where the bytes come back in as well.
class Thought(BaseModel):
    kind: Literal["thought"]
    text: str

    model_config = ConfigDict(extra="forbid")


class RowOpened(BaseModel):
    kind: Literal["row_opened"]
    call_id: str
    label: str
    symbol: str
    depth: int

    model_config = ConfigDict(extra="forbid")


class RowClosed(BaseModel):
    kind: Literal["row_closed"]
    call_id: str
    label: str
    outcome: Literal["done", "empty", "stopped", "failed"]
    detail: str
    depth: int

    model_config = ConfigDict(extra="forbid")


# The last line a run writes, and the only one that says how it went.
# Each ending names exactly what the workflow answered with, so the
# process folding the journal rebuilds that answer rather than reading
# a claim about it: there is no `commit` to trust on an ending that
# reports no commit.
class Conclusion(BaseModel):
    kind: Literal["conclusion"]
    ending: Literal["committed", "unchanged", "ambiguities", "stopped", "exhausted", "blocked", "failed"]
    commit: str = ""
    ambiguities: tuple[str, ...] = ()
    detail: str = ""

    model_config = ConfigDict(extra="forbid")


class Record(RootModel[Thought | RowOpened | RowClosed | Conclusion]): ...


# A generation of the specifications, running in a process of its own
# so that the window that asked for it is not what keeps it alive. The
# two processes share nothing but the run directory: the runner appends
# to the journal and never opens the session, and whoever is watching
# reads the journal and never writes it.
class Generation:
    POLL = 0.1
    # How long a runner that has written its ending is given to reach
    # the exit that frees its lock.
    FREED_WITHIN = 5.0
    # A runner has to import JRI before it can write anything, which is
    # a second or so cold and more on a machine under load. What this
    # bounds is a child that never gets there at all.
    STARTS_WITHIN = 60.0
    # Enough of the runner's own error output to recognise a crash by,
    # in a message that still fits on a screen.
    REPORTED_ERROR_BYTES = 2000

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        # Every file a run owns, and no other: the directory's own
        # ignore rule and the draft belong to the workspace and to
        # `Specs`, and outlive any one run.
        self.journal_file = workspace.root / paths.JOURNAL_FILE
        self.cancel_file = workspace.root / paths.CANCEL_FILE
        self.runner_log_file = workspace.root / paths.RUNNER_LOG_FILE
        self.lock = Lock(workspace.root / paths.GENERATION_LOCK_FILE)

    # The runner's whole life. It answers to the run directory and to
    # nothing else: the lock says it is alive, the journal says what it
    # did, and the cancel file is the only thing it listens to.
    @classmethod
    def execute(cls, settings: Settings) -> None:
        generation = cls(Workspace.find())
        # The directory is opened through the workspace, so the rule
        # that hides it from Git is in place before the lock file and
        # the first line of the journal are.
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

    # Whether a run has left a record here that nothing has folded yet.
    # A run in flight and a run whose window died before it could read
    # the ending are one state on disk, and one state to the caller:
    # both are followed, and the follower is what tells them apart.
    @property
    def exists(self) -> bool:
        return self.journal_file.exists()

    # What the workflow answered, as the journal spells it. A failure
    # is classified here and nowhere else, so the process that folds
    # the journal derives the turn's ending from an exception class
    # exactly as it would have from the workflow itself.
    @staticmethod
    def record(
        settings: Settings, cancelled: threading.Event
    ) -> Generator["specs_generation.Progress", None, Conclusion]:
        try:
            result = yield from specs_generation.generate(settings, cancelled)
        except UsageLimitError as error:
            logger.exception("generation_exhausted")
            return Conclusion(kind="conclusion", ending="exhausted", detail=str(error))
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
            case specs_generation.functional_analyst.Ambiguities():
                return Conclusion(kind="conclusion", ending="ambiguities", ambiguities=tuple(result.ambiguities))

    def start(self) -> None:
        self.workspace.open_generation_dir()
        try:
            # A run whose lock is held is a run in flight, and nothing
            # here is its to take away or to start beside.
            if self.lock.is_held():
                raise PersistenceError("A generation is already running in this project.")
            # Every leftover of a run already folded, the cancel file
            # included: a stale one would stop the run about to start
            # before it had done anything.
            self.discard()
            # The runner writes its own log through `logs.configure`,
            # so what lands here is only what escaped it: an import
            # failure, a library writing to standard error, a traceback.
            with self.runner_log_file.open("wb") as errors:
                process = subprocess.Popen(
                    [sys.executable, *RUNNER_COMMAND],
                    cwd=self.workspace.root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
                    **_detach(),
                )
        # A run directory JRI cannot write in is a fact about the
        # project rather than a crash of the run's, and no run started,
        # so there is nothing to take back.
        except (LockError, OSError) as error:
            logger.exception("generation_spawn_failed root=%r", self.workspace.root)
            raise PersistenceError(
                f"Could not start the generation in `{self.workspace.generation_dir}`: {error}"
            ) from error
        logger.info("generation_spawned pid=%d", process.pid)
        # The child is waited for, rather than assumed: a runner that
        # cannot start would otherwise be reported as a run in flight,
        # and the first poll of a journal that does not exist yet reads
        # as a run whose process is gone.
        deadline = time.monotonic() + self.STARTS_WITHIN
        while not self.exists:
            if process.poll() is not None:
                raise Error(f"JRI could not start the generation. {self._read_errors()}")
            if time.monotonic() >= deadline:
                raise Error("JRI could not start the generation: it never wrote anything down.")
            time.sleep(self.POLL)

    # Everything the run said about itself, in the order it said it,
    # from the first line of the journal however late this arrives.
    # What a stop does here is write the cancel file: the run is in
    # another process, so the only thing that can end it is the run
    # itself reading that it was asked to.
    def follow(
        self, cancelled: threading.Event | None = None, detached: threading.Event | None = None
    ) -> Generator["specs_generation.Progress", None, "specs_generation.SpecsResult | None"]:
        requested = False
        thought = ""
        for record in self._read():
            # A stop the follower was asked for is handed on before the
            # window leaving can end the following, so closing a window
            # right after asking still stops the run.
            if cancelled is not None and cancelled.is_set() and not requested:
                self.cancel_file.touch()
                requested = True
                logger.info("generation_stop_requested")
            # The window is gone and the run is not its to take with
            # it. Nothing here is folded or unlinked: the journal is
            # the record, and the next window reads it from the top.
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
                self.discard()
                return _answer(record)
            if record is not None:
                yield _replay(record)
        # Nothing is left folded here: the reader comes back with
        # `None` on the pass before the one it ends on, so a run of
        # thoughts is always flushed by the loop above.
        # The lock came free with no ending written, so the process
        # that was writing this is gone and nothing will finish it.
        self.discard()
        raise Error("The generation stopped before it finished, and its process is gone. Try again.")

    # What is left of a run whose record has been read. Only the files
    # this run wrote go, and only by name: the lock is left where it
    # is, since a file another process is about to lock is not this
    # one's to unlink, and `.gitignore` and `draft.patch` are the
    # directory's and the draft's rather than the run's. What says
    # nothing else is using these is the lock coming free -- the
    # operating system drops it when the runner dies -- and one JRI
    # holding the project, so no second follower is reading them. The
    # wait is for the moment between a run writing its ending, which is
    # the last thing it writes and what brings this call, and its
    # process getting as far as exiting: a record left behind over
    # those milliseconds is one the next run would attach to instead of
    # starting. A lock still held past the wait is a run still writing,
    # and none of this is its to take away.
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

    # Every line the journal has grown, one at a time, and `None` on
    # each pass that found nothing new. Only whole lines are read: a
    # run the operating system killed mid-append leaves a partial line,
    # and what makes it partial is the newline that is not there yet.
    def _read(self) -> Generator[Thought | RowOpened | RowClosed | Conclusion | None]:
        pending = b""
        number = 0
        last = False
        with self.journal_file.open("rb") as journal:
            while True:
                lines = (pending + journal.read()).split(b"\n")
                pending = lines.pop()
                for line in lines:
                    try:
                        yield _read_line(line, number)
                    # A record nothing can read ends a run as surely as
                    # one that says it failed, and a journal left
                    # behind would meet every run after this one with
                    # the same refusal.
                    except Error:
                        self.discard()
                        raise
                    number += 1
                if last:
                    return
                # A pass that found nothing is still news: most of a
                # run is one model call saying nothing for minutes, and
                # a reader that only came back with a record would hold
                # a stop for as long as the call did.
                yield None
                # Read once more before giving up: the runner writes
                # its ending and only then lets the lock go, so a lock
                # that came free between the read above and this has an
                # ending waiting in the file.
                last = not self.lock.is_held()
                if not last:
                    time.sleep(self.POLL)

    def _read_errors(self) -> str:
        try:
            errors = self.runner_log_file.read_bytes()[-self.REPORTED_ERROR_BYTES :]
        except OSError:
            return "It wrote nothing about why."
        return errors.decode(errors="replace").strip() or "It wrote nothing about why."


# What the workflow answered, rebuilt from the ending the run recorded.
# Every failure comes back as the exception class it left the workflow
# as, so the turn's ending is derived where it always was.
def _answer(conclusion: Conclusion) -> "specs_generation.SpecsResult | None":
    match conclusion.ending:
        case "committed":
            return conclusion.commit
        case "unchanged":
            return specs_generation.Unchanged()
        case "ambiguities":
            return specs_generation.functional_analyst.Ambiguities(
                outcome="ambiguities", ambiguities=list(conclusion.ambiguities)
            )
        case "stopped":
            return None
        case "exhausted":
            raise UsageLimitError(conclusion.detail)
        case "blocked":
            raise RepositoryStateError(conclusion.detail)
        case "failed":
            raise Error(conclusion.detail)


def _append(journal: IO[bytes], record: BaseModel, *, sync: bool = False) -> None:
    journal.write(record.model_dump_json().encode() + b"\n")
    # Flushed on every record so a reader sees it, and synced on the
    # two that a crash must not lose: the opening, which is what says a
    # run started at all, and the ending, which is what says how it
    # went. Syncing every one of twelve thousand records measured 6.6 s
    # against 0.02 s, for a guarantee the reader already has: a partial
    # last line is tolerated, and a lost tail reads as the interruption
    # it was.
    journal.flush()
    if sync:
        os.fsync(journal.fileno())


# Popen's own detaching, spelled for each platform: CPython ignores
# `start_new_session` on Windows, where the flags are the only thing
# that puts the runner outside the console the window is in.
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
                kind="row_opened", call_id=event.call_id, label=event.label, symbol=event.symbol, depth=event.depth
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
    # A line JRI cannot read is a line JRI did not write, and a run
    # nobody can read the record of is one nobody can report.
    except ValidationError as error:
        logger.info("generation_record_unreadable line=%d", number)
        raise Error("JRI could not read what this generation wrote down. Try again.") from error


# A thought never arrives here: the run of them a journal holds is
# folded into one delta where it is read, exactly as the recording
# folds a run of them while a run is watched.
def _replay(record: RowOpened | RowClosed) -> "specs_generation.Progress":
    match record:
        case RowOpened():
            return ToolCallStarted(record.call_id, record.label, record.symbol, depth=record.depth)
        case RowClosed():
            return ToolCallFinished(
                record.call_id, record.label, record.outcome, detail=record.detail, depth=record.depth
            )


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


# The whole record of one run. It opens with the header, so a journal
# that exists is a run that started; it closes with the conclusion, so
# a journal that ends without one is a run whose process is gone. `for`
# drops what a generator returns, and what a run returns is exactly
# that conclusion, so the events are pulled one at a time. Truncating
# whatever is here: the lock this process holds is what says no other
# run is writing, and a journal a folded run left is a record nothing
# will read again.
def _write_journal(path: Path, events: Generator["specs_generation.Progress", None, Conclusion]) -> None:
    logger.info("generation_started pid=%d", os.getpid())
    with path.open("wb") as journal:
        _append(journal, Header(version=__version__, pid=os.getpid(), started=_stamp()), sync=True)
        while True:
            try:
                event = next(events)
            except StopIteration as ending:
                conclusion = cast("Conclusion", ending.value)
                break
            _append(journal, _describe(event))
        _append(journal, conclusion, sync=True)
    logger.info("generation_finished ending=%s", conclusion.ending)


# A stop crosses to the run as a file rather than as a signal: the
# runner is in a process group of its own on every platform, and
# Windows has no signal that would reach it there.
def _watch_cancellation(cancel_file: Path, cancelled: threading.Event, stopping: threading.Event) -> None:
    while not stopping.wait(Generation.POLL / 2):
        if cancel_file.exists():
            logger.info("generation_stop_read")
            cancelled.set()
            return
