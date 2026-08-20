import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO

import pytest

from jri.core import paths
from jri.core.ai import ReasoningDelta, ToolCallStarted
from jri.core.exceptions import (
    Error,
    PersistenceError,
    ProviderRefusalError,
    ProviderUnavailableError,
    RepositoryStateError,
    RunDetached,
    UsageLimitError,
)
from jri.core.generation import Conclusion, Generation
from jri.core.workspace import Workspace
from jri.lib.lock import Lock
from tests.conftest import CreateRepository, RunGit
from tests.doubles.generation import ConcludingLock
from tests.doubles.lock import OWN_PID, hold, read_fork_child, watch_a_process_go
from tests.doubles.openai import FakeClient
from tests.doubles.settings import build_settings
from tests.doubles.specs_generation import (
    COMMIT,
    FINISHED_ROW,
    STARTED_ROW,
    STREAMED_THOUGHT,
    STREAMED_THOUGHTS,
    THOUGHT,
    generate_blocked,
    generate_failing,
    generate_refused,
    generate_silently,
    generate_stopped,
    generate_streaming,
    generate_succeeding,
    generate_thinking,
)
from tests.doubles.workspace import hold_workspace, install_workspace

# A large age, well past WRITTEN_WITHIN, so a call that has been open
# a while cannot be mistaken for one that just opened.
AGED = 360.0
# A run holds its reasoning for one poll. A poll this long lasts more than
# the run, thus only the flush before the ending can write a held batch.
BATCHES_FOR = 60.0
CONCLUDES_WITHIN = 60.0
# A crash writes its cause last, under more output than JRI keeps.
CRASHING_RUNNER = f"""
import sys

sys.stderr.write("the warnings it began with\\n")
sys.stderr.write("x" * {Generation.REPORTED_ERROR_BYTES})
sys.stderr.write("\\nthe runner fell over")
sys.exit(1)
"""
DRAFT = b"the work the run before this one saved\n"
# A process ends the moment the signal reaches it. Only a machine under load uses any of this time.
ENDS_WITHIN = 5.0
# This is the largest number a run reads as a process, and no process on this machine wears it. A halt aimed at
# it reaches nothing, thus the lock behind it stays held.
GONE_PID = 2147483647
# This is the variable that names the window a runner belongs to. The test writes it out, so a change of the name
# shows here as a failure.
HOLDER_VARIABLE = "JRI_HOLDER"
LIVE_JOURNAL = b"what the run that holds the lock wrote\n"
LIVE_LOG = b"what the run that holds the lock could not journal\n"
# This stands in for a runner that writes down the window it was told it belongs to.
MARKING_RUNNER = f"""
import os
from pathlib import Path

journal = Path({paths.JOURNAL_FILE!r})
written = journal.with_name("written")
written.write_bytes(os.environ.get({HOLDER_VARIABLE!r}, "").encode() + b"\\n")
written.replace(journal)
"""
# `RunDetached` is the signal that the window left, and not a failure. It carries no wording at all, and this
# pattern holds it to that.
NO_WORDING = "^$"
POLL = 0.01
RUNNER_JOURNAL = b"what the run that started now wrote\n"
# This stands in for a runner that starts and writes its journal. It writes
# under another name and renames, thus a reader that waits for the journal
# never reads a file that is still incomplete.
RUNNER = f"""
from pathlib import Path

journal = Path({paths.JOURNAL_FILE!r})
written = journal.with_name("written")
written.write_bytes({RUNNER_JOURNAL!r})
written.replace(journal)
"""
STARTER = """
import sys, time
from pathlib import Path
from jri.core.generation import Generation
from jri.core.workspace import Workspace

root, ready = Path(sys.argv[1]), Path(sys.argv[2])
Generation(Workspace(root)).spawn()
ready.touch()
time.sleep(60)
"""
SETTINGS = "llm:\n  provider: http://127.0.0.1:9/v1\n  api_key: JRI_TEST_API_KEY\nlogging:\n  level: CRITICAL\n"
STARTS_WITHIN = 60.0
STOPS_AFTER = 0.5
# This is one more than the largest number a run can read as a process. A record of it names no process, thus a
# halt has nothing to end.
TOO_LARGE_PID = 2147483648
# These are the two numbers below the first real process. Neither names a runner, and a signal sent to either one
# leaves the run it was aimed at alone and ends the session around it instead: 0 means the group of the caller,
# and 1 means every process the user owns. A runner inside a container writes 1 into the lock file it shares with
# the host, so a halt run on the host reads one of these from a record JRI really wrote.
SESSION_WIDE_PIDS = (0, 1)
WRITTEN_WITHIN = 30.0


def build_generation(tmp_path: Path) -> Generation:
    install_workspace(tmp_path)
    return Generation(Workspace(tmp_path))


def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workflow: object) -> Generation:
    generation = build_generation(tmp_path)
    monkeypatch.setattr("jri.core.generation.specs_generation.generate", workflow)
    Generation.execute(build_settings(FakeClient([])))
    return generation


def write_journal(tmp_path: Path, *lines: str) -> Generation:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()
    generation.journal_file.write_bytes("".join(f"{line}\n" for line in lines).encode())
    return generation


def read_journal(generation: Generation) -> list[dict[str, object]]:
    return [json.loads(line) for line in generation.journal_file.read_text(encoding="utf-8").splitlines()]


# A folded run answers with the return of its follower, and a `for` loop drops that return.
def read_answer(events: Generator[object, None, object]) -> object:
    try:
        while True:
            next(events)
    except StopIteration as ending:
        return ending.value


# This is the full life of a runner, in a thread of this process. It takes the same lock, writes the same journal,
# and hears a stop through the same file. The body runs while that run is under way, and the run ends with it.
@contextmanager
def start_a_run(generation: Generation) -> "Iterator[None]":
    generation.workspace.open_generation_dir()
    runner = threading.Thread(target=Generation.execute, args=(build_settings(FakeClient([])),), daemon=True)
    runner.start()
    while not generation.exists:
        time.sleep(POLL)
    try:
        yield
    finally:
        runner.join(timeout=CONCLUDES_WITHIN)
    assert not runner.is_alive()


def write_header(*, pid: int = 1) -> str:
    return json.dumps({"version": "0", "pid": pid, "started": datetime.now(UTC).isoformat()})


def write_row(started: object, *, call_id: str = "commit", label: str = "Saving") -> str:
    return json.dumps({
        "kind": "row_opened",
        "call_id": call_id,
        "label": label,
        "symbol": "💾",
        "depth": 0,
        "started": started,
    })


# This mimics the Windows failure a real open handle causes, so the
# guard against it runs on every platform, not only Windows.
def refuse_removing_an_open_file(monkeypatch: pytest.MonkeyPatch) -> None:
    handles: list[IO[bytes]] = []
    open_file, unlink = Path.open, Path.unlink

    def track(self: Path, mode: str = "r", *arguments: object, **keywords: object) -> IO[bytes]:
        handle = open_file(self, mode, *arguments, **keywords)  # type: ignore[call-overload]
        handles.append(handle)
        return handle

    def refuse(self: Path, *, missing_ok: bool = False) -> None:
        if any(handle.name == str(self) and not handle.closed for handle in handles):
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "open", track)
    monkeypatch.setattr(Path, "unlink", refuse)


def test_writes_every_event_a_run_produced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = run(tmp_path, monkeypatch, generate_thinking)

    header, *records = read_journal(generation)

    assert header["pid"] == os.getpid()
    opened = datetime.fromisoformat(str(records[0].pop("started")))
    assert 0 <= (datetime.now(UTC) - opened).total_seconds() < WRITTEN_WITHIN
    assert records == [
        {"kind": "row_opened", "call_id": "commit", "label": STARTED_ROW.label, "symbol": "💾", "depth": 0},
        {"kind": "thought", "text": THOUGHT.text},
        {
            "kind": "row_closed",
            "call_id": "commit",
            "label": FINISHED_ROW.label,
            "outcome": "done",
            "detail": "",
            "depth": 0,
        },
        {"kind": "conclusion", "ending": "committed", "commit": COMMIT, "ambiguities": [], "detail": ""},
    ]


def test_writes_the_reasoning_a_run_streams_as_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = run(tmp_path, monkeypatch, generate_streaming)

    _, *records = read_journal(generation)

    thoughts = [record for record in records if record["kind"] == "thought"]
    assert 0 < len(thoughts) < STREAMED_THOUGHTS
    streamed = "".join(STREAMED_THOUGHT.format(number=number) for number in range(STREAMED_THOUGHTS))
    assert "".join(str(thought["text"]) for thought in thoughts) == streamed


def test_writes_the_reasoning_a_run_streams_while_a_row_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def generate_slowly(_settings: object, _cancelled: object = None) -> Generator[object, None, str]:
        yield STARTED_ROW
        yield ReasoningDelta("Weighing ")
        # Wait longer than one poll. The reasoning after this wait belongs to a later batch.
        time.sleep(Generation.POLL * 2)
        yield ReasoningDelta("the options")
        yield ReasoningDelta(", carefully.")
        yield FINISHED_ROW
        return COMMIT

    generation = run(tmp_path, monkeypatch, generate_slowly)

    _, *records = read_journal(generation)

    thoughts = [record for record in records if record["kind"] == "thought"]
    assert len(thoughts) > 1
    assert "".join(str(thought["text"]) for thought in thoughts) == "Weighing the options, carefully."


def test_writes_the_reasoning_a_run_still_held_when_it_ended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def generate_concluding(_settings: object, _cancelled: object = None) -> Generator[object, None, str]:
        yield STARTED_ROW
        yield THOUGHT
        return COMMIT

    monkeypatch.setattr(Generation, "POLL", BATCHES_FOR)

    generation = run(tmp_path, monkeypatch, generate_concluding)

    _, *records = read_journal(generation)

    assert records[-2:] == [
        {"kind": "thought", "text": THOUGHT.text},
        {"kind": "conclusion", "ending": "committed", "commit": COMMIT, "ambiguities": [], "detail": ""},
    ]


def test_reads_back_the_events_a_journal_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = run(tmp_path, monkeypatch, generate_thinking)

    events = generation.follow()
    replayed = list(events)

    opened = replayed[0]
    assert isinstance(opened, ToolCallStarted)
    assert replace(opened, age=0.0) == STARTED_ROW
    assert opened.age < WRITTEN_WITHIN
    assert replayed[1:] == [THOUGHT, FINISHED_ROW]


@pytest.mark.parametrize(
    ("workflow", "expected"), [(generate_succeeding, COMMIT), (generate_stopped, None)], ids=["committed", "stopped"]
)
def test_reads_back_what_a_run_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workflow: object, expected: str | None
) -> None:
    cancelled = threading.Event()
    generation = build_generation(tmp_path)
    monkeypatch.setattr("jri.core.generation.specs_generation.generate", workflow)

    with start_a_run(generation):
        # The run is under way now. A stop asked for before it started is a stop the run removes, not one it hears.
        cancelled.set()
        answer = read_answer(generation.follow(cancelled))

    assert answer == expected


# A runner writes its ending and only then frees its lock. The follower that meets that free lock still has the
# ending in front of it, unread.
def test_reads_back_the_ending_a_run_wrote_before_it_freed_its_lock(tmp_path: Path) -> None:
    generation = write_journal(tmp_path, write_header(), write_row(datetime.now(UTC).isoformat()))
    generation.lock = ConcludingLock(
        generation.lock.path,
        generation.journal_file,
        json.dumps({"kind": "conclusion", "ending": "committed", "commit": COMMIT}).encode() + b"\n",
    )

    answer = read_answer(generation.follow())

    assert answer == COMMIT


@pytest.mark.parametrize(
    ("workflow", "error", "message"),
    [
        (generate_blocked, RepositoryStateError, "Your project has uncommitted changes."),
        (generate_failing, Error, "The architect could not be reached."),
        (generate_refused, ProviderRefusalError, "400 Bad Request"),
    ],
    ids=["blocked", "failed", "refused"],
)
def test_names_the_failure_a_run_ended_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workflow: object, error: type[Exception], message: str
) -> None:
    generation = run(tmp_path, monkeypatch, workflow)

    with pytest.raises(error, match=message):
        list(generation.follow())


def test_names_a_spent_budget_a_run_could_not_finish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def generate_exhausted(_settings: object, _cancelled: object = None) -> object:
        raise UsageLimitError("The plan's usage limit was reached.")
        yield  # type: ignore[unreachable]

    generation = run(tmp_path, monkeypatch, generate_exhausted)

    with pytest.raises(UsageLimitError, match="usage limit"):
        list(generation.follow())


def test_names_a_provider_a_run_could_not_reach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def generate_unavailable(_settings: object, _cancelled: object = None) -> Iterator[object]:
        yield STARTED_ROW
        raise ProviderUnavailableError("The provider answered nothing this run could use.")

    generation = run(tmp_path, monkeypatch, generate_unavailable)

    with pytest.raises(ProviderUnavailableError, match="answered nothing"):
        list(generation.follow())


# A run can fail with a class that no clause names. That failure is an ending too, and the journal must get it.
def test_names_an_unexpected_failure_a_run_ended_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def generate_erring(_settings: object, _cancelled: object = None) -> Iterator[object]:
        yield STARTED_ROW
        raise ValueError("The architect answered with no plan in it.")

    generation = run(tmp_path, monkeypatch, generate_erring)

    with pytest.raises(Error, match="no plan in it"):
        list(generation.follow())


def test_folds_the_deltas_a_backlog_holds_into_one(tmp_path: Path) -> None:
    thoughts = "".join(json.dumps({"kind": "thought", "text": f"part {number} "}) + "\n" for number in range(200))
    generation = write_journal(
        tmp_path, write_header(), *thoughts.splitlines(), json.dumps({"kind": "conclusion", "ending": "unchanged"})
    )

    replayed = list(generation.follow())

    assert replayed == [ReasoningDelta("".join(f"part {number} " for number in range(200)))]


def test_ignores_the_partial_line_a_killed_writer_left(tmp_path: Path) -> None:
    generation = write_journal(tmp_path, write_header(), write_row(datetime.now(UTC).isoformat()))
    with generation.journal_file.open("ab") as journal:
        journal.write(b'{"kind": "thou')

    replayed: list[object] = []
    events = generation.follow()
    with pytest.raises(Error, match="stopped before it finished"):
        replayed.extend(events)

    assert [replace(event, age=0.0) if isinstance(event, ToolCallStarted) else event for event in replayed] == [
        ToolCallStarted("commit", "Saving", "💾")
    ]


def test_counts_an_open_row_from_when_its_call_began(tmp_path: Path) -> None:
    generation = write_journal(
        tmp_path, write_header(), write_row((datetime.now(UTC) - timedelta(seconds=AGED)).isoformat())
    )

    replayed: list[object] = []
    with pytest.raises(Error, match="stopped before it finished"):
        replayed.extend(generation.follow())

    opened = replayed[0]
    assert isinstance(opened, ToolCallStarted)
    assert AGED <= opened.age < AGED + WRITTEN_WITHIN


def test_counts_a_row_a_moved_clock_dated_ahead_of_the_reading_from_now(tmp_path: Path) -> None:
    generation = write_journal(
        tmp_path, write_header(), write_row((datetime.now(UTC) + timedelta(seconds=AGED)).isoformat())
    )

    replayed: list[object] = []
    with pytest.raises(Error, match="stopped before it finished"):
        replayed.extend(generation.follow())

    opened = replayed[0]
    assert isinstance(opened, ToolCallStarted)
    assert not opened.age


def test_refuses_a_row_whose_start_names_no_zone(tmp_path: Path) -> None:
    generation = write_journal(tmp_path, write_header(), write_row(datetime.now(UTC).replace(tzinfo=None).isoformat()))

    with pytest.raises(Error, match="could not read what this generation wrote down"):
        list(generation.follow())


def test_reports_a_run_whose_writer_died_as_interrupted(tmp_path: Path) -> None:
    generation = write_journal(
        tmp_path, write_header(), json.dumps({"kind": "thought", "text": "Weighing the options."})
    )

    replayed: list[object] = []
    events = generation.follow()
    with pytest.raises(Error, match="stopped before it finished"):
        replayed.extend(events)

    assert replayed == [ReasoningDelta("Weighing the options.")]


def test_refuses_a_text_delta_a_journal_claims_a_run_produced(tmp_path: Path) -> None:
    generation = write_journal(
        tmp_path, write_header(), json.dumps({"kind": "text", "text": "I have written your specifications."})
    )

    with pytest.raises(Error, match="could not read"):
        list(generation.follow())


def test_forgets_the_record_of_a_run_it_folded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = run(tmp_path, monkeypatch, generate_succeeding)
    generation.cancel_file.write_bytes(b"")
    generation.runner_log_file.write_bytes(b"")

    list(generation.follow())

    assert not generation.exists
    assert not generation.cancel_file.exists()
    assert not generation.runner_log_file.exists()
    assert generation.workspace.generation_dir.is_dir()
    assert f"/{paths.GENERATION_DIR.rpartition('/')[2]}/" in generation.workspace.gitignore_file.read_text()


def test_lets_go_of_the_journal_before_it_forgets_a_run_it_folded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = run(tmp_path, monkeypatch, generate_succeeding)
    refuse_removing_an_open_file(monkeypatch)

    list(generation.follow())

    assert not generation.exists


def test_lets_go_of_the_journal_before_it_forgets_a_record_it_could_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = write_journal(
        tmp_path, write_header(), json.dumps({"kind": "text", "text": "I have written your specifications."})
    )
    refuse_removing_an_open_file(monkeypatch)

    with pytest.raises(Error, match="could not read"):
        list(generation.follow())

    assert not generation.exists


def test_forgets_the_worktrees_a_killed_run_left(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)
    leftovers = (tmp_path / paths.WORKTREE_DIR, tmp_path / paths.SNAPSHOT_DIR, tmp_path / paths.PRE_IMAGE_DIR)
    for leftover in leftovers:
        leftover.mkdir(parents=True)
        (leftover / "main.py").write_text("what a killed run was reading\n", encoding="utf-8")

    generation.discard()

    assert not any(leftover.exists() for leftover in leftovers)


def test_keeps_the_worktree_of_a_run_still_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()
    worktree = tmp_path / paths.WORKTREE_DIR
    worktree.mkdir(parents=True)
    monkeypatch.setattr(Generation, "FREED_WITHIN", 0.2)

    with hold(tmp_path / paths.GENERATION_LOCK_FILE):
        generation.discard()

    assert worktree.exists()


def test_keeps_the_record_of_a_run_still_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = write_journal(tmp_path, write_header())
    monkeypatch.setattr(Generation, "FREED_WITHIN", 0.2)

    with hold(tmp_path / paths.GENERATION_LOCK_FILE):
        generation.discard()

    assert generation.exists


def test_stops_a_run_the_other_side_asked_to_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = threading.Event()
    generation = build_generation(tmp_path)
    monkeypatch.setattr("jri.core.generation.specs_generation.generate", generate_stopped)
    generation.workspace.open_generation_dir()
    runner = threading.Thread(target=Generation.execute, args=(build_settings(FakeClient([])),), daemon=True)
    runner.start()
    while not generation.exists:
        time.sleep(POLL)

    events = generation.follow(cancelled)
    next(events)
    cancelled.set()
    answer = read_answer(events)

    assert answer is None
    runner.join(timeout=CONCLUDES_WITHIN)
    assert not runner.is_alive()


def test_stops_a_run_that_is_saying_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = threading.Event()
    generation = build_generation(tmp_path)
    monkeypatch.setattr("jri.core.generation.specs_generation.generate", generate_silently)
    generation.workspace.open_generation_dir()
    runner = threading.Thread(target=Generation.execute, args=(build_settings(FakeClient([])),), daemon=True)
    runner.start()
    while not generation.exists:
        time.sleep(POLL)

    threading.Timer(STOPS_AFTER, cancelled.set).start()
    answer = read_answer(generation.follow(cancelled))

    assert answer is None
    runner.join(timeout=CONCLUDES_WITHIN)
    assert not runner.is_alive()


def test_leaves_a_run_going_when_the_window_watching_it_leaves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    detached = threading.Event()
    generation = build_generation(tmp_path)
    monkeypatch.setattr("jri.core.generation.specs_generation.generate", generate_stopped)
    generation.workspace.open_generation_dir()
    runner = threading.Thread(target=Generation.execute, args=(build_settings(FakeClient([])),), daemon=True)
    runner.start()
    while not generation.exists:
        time.sleep(POLL)

    events = generation.follow(None, detached)
    next(events)
    detached.set()
    with pytest.raises(RunDetached, match=NO_WORDING):
        list(events)

    assert runner.is_alive()
    assert generation.exists
    assert not generation.cancel_file.exists()
    generation.cancel_file.touch()
    runner.join(timeout=CONCLUDES_WITHIN)
    assert not runner.is_alive()
    assert read_journal(generation)[-1]["kind"] == "conclusion"


def test_hands_on_a_stop_the_window_asked_for_before_it_left(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled, detached = threading.Event(), threading.Event()
    generation = build_generation(tmp_path)
    monkeypatch.setattr("jri.core.generation.specs_generation.generate", generate_silently)
    generation.workspace.open_generation_dir()
    runner = threading.Thread(target=Generation.execute, args=(build_settings(FakeClient([])),), daemon=True)
    runner.start()
    while not generation.exists:
        time.sleep(POLL)

    def leave() -> None:
        cancelled.set()
        detached.set()

    threading.Timer(STOPS_AFTER, leave).start()
    with pytest.raises(RunDetached, match=NO_WORDING):
        list(generation.follow(cancelled, detached))

    assert generation.cancel_file.exists()
    runner.join(timeout=CONCLUDES_WITHIN)
    assert not runner.is_alive()
    assert read_journal(generation)[-1]["ending"] == "stopped"


def test_refuses_a_second_run_while_one_holds_the_lock(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()
    generation.journal_file.write_bytes(LIVE_JOURNAL)
    generation.runner_log_file.write_bytes(LIVE_LOG)

    with hold(tmp_path / paths.GENERATION_LOCK_FILE), pytest.raises(PersistenceError, match="already running"):
        generation.spawn()

    assert generation.journal_file.read_bytes() == LIVE_JOURNAL
    assert generation.runner_log_file.read_bytes() == LIVE_LOG


def test_refuses_a_runner_while_one_holds_the_lock(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()
    generation.journal_file.write_bytes(LIVE_JOURNAL)

    with hold(tmp_path / paths.GENERATION_LOCK_FILE), pytest.raises(PersistenceError, match="already running"):
        Generation.execute(build_settings(FakeClient([])))

    assert generation.journal_file.read_bytes() == LIVE_JOURNAL


# A window owns the conversation that a run reports to. A run started beside that window would report to a
# conversation that asked for nothing.
def test_refuses_a_runner_beside_the_window_that_has_the_project(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)

    with (
        hold_workspace(tmp_path) as window,
        pytest.raises(PersistenceError, match=f"window holds this project, in the window running process {window.pid}"),
    ):
        Generation.execute(build_settings(FakeClient([])))

    assert not generation.workspace.generation_dir.exists()


# This is the runner of that window: it names the window it belongs to, and the window let it start.
def test_runs_a_runner_the_window_that_has_the_project_named_itself_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = build_generation(tmp_path)
    monkeypatch.setattr("jri.core.generation.specs_generation.generate", generate_succeeding)

    with hold_workspace(tmp_path) as window:
        monkeypatch.setenv(HOLDER_VARIABLE, str(window.pid))
        Generation.execute(build_settings(FakeClient([])))

    assert read_journal(generation)[-1]["ending"] == "committed"


# A cancel file left by the run before this one would stop this run the instant it begins.
def test_forgets_the_stop_a_folded_run_left_before_it_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def generate_watching_for_a_stop(
        _settings: object, cancelled: threading.Event | None = None
    ) -> Generator[object, None, str | None]:
        yield STARTED_ROW
        assert cancelled is not None
        # A stop that stands in the project reaches the run inside this wait.
        return None if cancelled.wait(STOPS_AFTER) else COMMIT

    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()
    generation.cancel_file.touch()
    monkeypatch.setattr("jri.core.generation.specs_generation.generate", generate_watching_for_a_stop)

    Generation.execute(build_settings(FakeClient([])))

    assert read_journal(generation)[-1]["ending"] == "committed"
    assert not generation.cancel_file.exists()


# A run without a window says how it went through the status of its process. Each ending it could do nothing
# about is a failed process.
@pytest.mark.parametrize("ending", ["exhausted", "refused", "unavailable", "blocked", "failed"])
def test_states_that_the_run_could_not_do_the_work(ending: str) -> None:
    assert Conclusion.model_validate({"kind": "conclusion", "ending": ending}).failure


# A stopped run did what it was told to do, and a run that found something to clarify did its work and asks a
# question about it. Neither is a failed process.
@pytest.mark.parametrize("ending", ["committed", "unchanged", "ambiguities", "stopped"])
def test_states_that_the_run_did_the_work(ending: str) -> None:
    assert not Conclusion.model_validate({"kind": "conclusion", "ending": ending}).failure


# A run without a window has no journal left to read by the time its caller could read one. The conclusion it
# answers with is the only thing that says how it went.
def test_answers_with_the_conclusion_the_run_wrote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_generation(tmp_path)
    monkeypatch.setattr("jri.core.generation.specs_generation.generate", generate_refused)

    conclusion = Generation.execute(build_settings(FakeClient([])))

    assert conclusion.ending == "refused"


def test_forgets_what_a_folded_run_left_before_it_starts_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()
    generation.journal_file.write_bytes(b"what the run before this one wrote\n")
    generation.cancel_file.touch()
    worktree = tmp_path / paths.WORKTREE_DIR
    worktree.mkdir(parents=True)
    monkeypatch.setattr("jri.core.generation.RUNNER_COMMAND", ("-c", RUNNER))

    generation.spawn()

    assert generation.journal_file.read_bytes() == RUNNER_JOURNAL
    assert not generation.cancel_file.exists()
    assert not worktree.exists()


# A window spawns its runner while it holds the project. The runner reads this name to say which window let it run.
def test_names_the_window_that_spawned_a_runner_to_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = build_generation(tmp_path)
    monkeypatch.setattr("jri.core.generation.RUNNER_COMMAND", ("-c", MARKING_RUNNER))

    generation.spawn()

    assert generation.journal_file.read_bytes() == f"{os.getpid()}\n".encode()


def test_reports_a_run_log_it_cannot_open(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()
    generation.runner_log_file.mkdir()

    with pytest.raises(PersistenceError, match="Could not start the generation"):
        generation.spawn()


def test_reports_a_run_lock_it_cannot_open_rather_than_calling_the_run_over(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()
    generation.lock.path.mkdir()

    with pytest.raises(PersistenceError, match="Could not read the generation"):
        _ = generation.is_running


def test_reports_a_runner_that_could_not_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = build_generation(tmp_path)
    monkeypatch.setattr(
        "jri.core.generation.RUNNER_COMMAND",
        ("-c", "import sys; sys.stderr.write('the runner fell over'); sys.exit(1)"),
    )

    with pytest.raises(Error, match="the runner fell over"):
        generation.spawn()


# A crash names itself at the end of what it wrote. Report that end, and not the noise before it.
def test_reports_the_last_of_what_a_runner_that_crashed_wrote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = build_generation(tmp_path)
    monkeypatch.setattr("jri.core.generation.RUNNER_COMMAND", ("-c", CRASHING_RUNNER))

    with pytest.raises(Error, match=r"the runner fell over$") as failure:
        generation.spawn()

    assert str(failure.value).endswith("the runner fell over")
    assert "the warnings it began with" not in str(failure.value)


def test_reports_a_runner_that_never_wrote_anything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = build_generation(tmp_path)
    monkeypatch.setattr("jri.core.generation.RUNNER_COMMAND", ("-c", "import time; time.sleep(30)"))
    monkeypatch.setattr(Generation, "STARTS_WITHIN", 0.2)

    with pytest.raises(Error, match="never wrote anything down"):
        generation.spawn()


def test_asks_no_run_to_stop_when_none_is_going(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)

    assert not generation.stop()

    assert not generation.workspace.generation_dir.exists()


def test_asks_the_run_that_is_going_to_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = build_generation(tmp_path)
    monkeypatch.setattr("jri.core.generation.specs_generation.generate", generate_silently)

    with start_a_run(generation):
        assert generation.stop()

    assert read_journal(generation)[-1]["ending"] == "stopped"


def test_ends_no_run_when_none_is_going(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)

    assert not generation.halt()

    assert not generation.workspace.generation_dir.exists()


# A runner leads a session of its own, and Git and the provider run in the group of that session. A halt that
# ends the runner alone would leave those processes behind.
@pytest.mark.skipif(sys.platform == "win32", reason="a process group and a `SIGKILL` are POSIX")
def test_ends_the_run_that_is_going_and_the_processes_it_started(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()

    with hold(generation.lock.path, record=OWN_PID, forking=True, session=True) as runner:
        started = read_fork_child(generation.lock.path)

        assert generation.halt()

        # A killed process answers with the signal that ended it, and a process that ended by itself answers zero.
        assert runner.wait(timeout=ENDS_WITHIN)
        assert watch_a_process_go(started), "a process the run started is still running"


# A halt looks exactly like the machine dying, and a machine that dies removes nothing. The recovery for a run
# that never ended reads these files back.
@pytest.mark.skipif(sys.platform == "win32", reason="a `SIGKILL` is POSIX")
def test_keeps_everything_the_run_it_ended_left(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()
    generation.journal_file.write_bytes(LIVE_JOURNAL)
    generation.runner_log_file.write_bytes(LIVE_LOG)
    generation.cancel_file.touch()
    worktree = tmp_path / paths.WORKTREE_DIR
    worktree.mkdir(parents=True)

    with hold(generation.lock.path, record=OWN_PID, session=True):
        assert generation.halt()

    assert generation.journal_file.read_bytes() == LIVE_JOURNAL
    assert generation.runner_log_file.read_bytes() == LIVE_LOG
    assert generation.cancel_file.exists()
    assert worktree.exists()


# A record that JRI did not write names no process, whether it says nothing at all or says a number no process
# on this machine can wear.
@pytest.mark.parametrize("record", ["", str(TOO_LARGE_PID)], ids=["silent", "too large"])
def test_refuses_to_end_a_run_that_does_not_name_a_process(tmp_path: Path, record: str) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()

    with hold(generation.lock.path, record=record), pytest.raises(PersistenceError, match="without saying what it is"):
        generation.halt()


# A halt reads the lock file to learn which process to end. A record of 0 or 1 asks the kernel for the group of the
# caller or for every process the user owns, so honouring it ends the whole login session. Turn such a record down
# while it is still a record.
# Stand a double in for the signal. A gate that writes this refusal wrongly would otherwise really ask the kernel
# to end every process the runner owns, and take the run of the suite down with it.
@pytest.mark.parametrize("pid", SESSION_WIDE_PIDS, ids=["own group", "init"])
def test_refuses_to_end_a_run_that_names_a_session_wide_target(
    tmp_path: Path, pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()
    signalled: list[int] = []
    monkeypatch.setattr("jri.core.generation._kill", signalled.append)

    with (
        hold(generation.lock.path, record=str(pid)),
        pytest.raises(PersistenceError, match="Stop that container instead"),
    ):
        generation.halt()

    assert not signalled


# A run that no signal from here can aim at is still a run. A report that called it gone would send a supervisor
# to start a second one beside it.
@pytest.mark.parametrize("pid", SESSION_WIDE_PIDS, ids=["own group", "init"])
def test_reports_a_run_that_no_signal_can_aim_at(tmp_path: Path, pid: int) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()

    with hold(generation.lock.path, record=str(pid)):
        status = generation.read_status()

    assert status.pid == pid


def test_reports_a_run_that_would_not_let_its_lock_go(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()
    monkeypatch.setattr(Generation, "FREED_WITHIN", 0.2)

    with (
        hold(generation.lock.path, record=str(GONE_PID)),
        pytest.raises(PersistenceError, match=f"ended the generation process {GONE_PID}, and it still holds"),
    ):
        generation.halt()


def test_reports_the_run_that_is_going_and_the_step_it_reached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = build_generation(tmp_path)
    monkeypatch.setattr("jri.core.generation.specs_generation.generate", generate_stopped)

    with start_a_run(generation):
        # The header lands first, and the row of the step the run is in after that.
        while STARTED_ROW.label.encode() not in generation.journal_file.read_bytes():
            time.sleep(POLL)
        status = generation.read_status()
        generation.cancel_file.touch()

    assert status.pid == os.getpid()
    assert status.step == STARTED_ROW.label
    assert status.recorded
    assert not status.stopping
    assert status.started is not None
    assert 0 <= (datetime.now(UTC) - status.started).total_seconds() < WRITTEN_WITHIN


# A runner takes its lock before it writes its first journal line. A report of that moment says a run is alive and
# gives it no start time.
def test_reports_a_run_that_took_its_lock_before_it_wrote_a_journal(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()

    # Read the runner from the lock, and not from the process that started it. A Windows launcher runs the
    # interpreter in a process of its own, and the interpreter is the runner that takes the lock.
    with hold(generation.lock.path, record=OWN_PID):
        status = generation.read_status()
        recorded = Lock(generation.lock.path).holder

    assert status.pid == int(recorded)
    assert status.started is None
    assert not status.recorded


# A header that carries a start time which is not a time is not a header JRI wrote. A report reads what it can
# and says the run is alive, rather than falling over on the one command a broken project is read with.
def test_reports_a_run_whose_header_holds_no_time(tmp_path: Path) -> None:
    generation = write_journal(tmp_path, json.dumps({"version": "0", "pid": 1, "started": "now"}))

    with hold(generation.lock.path, record=OWN_PID):
        status = generation.read_status()

    assert status.started is None
    assert status.pid is not None


# A row that closed is a step the run finished with. The step it is in is the row it left open.
def test_reports_the_row_a_run_left_open_as_its_step(tmp_path: Path) -> None:
    started = datetime.now(UTC).isoformat()
    generation = write_journal(
        tmp_path,
        write_header(),
        write_row(started, call_id="explore", label="Studying"),
        write_row(started, call_id="commit", label="Saving"),
        json.dumps({
            "kind": "row_closed",
            "call_id": "commit",
            "label": "Saved",
            "outcome": "done",
            "detail": "",
            "depth": 0,
        }),
        json.dumps({"kind": "thought", "text": "Weighing the options."}),
    )

    status = generation.read_status()

    assert status.step == "Studying"


def test_reports_the_ending_no_window_folded(tmp_path: Path) -> None:
    generation = write_journal(
        tmp_path, write_header(), json.dumps({"kind": "conclusion", "ending": "committed", "commit": COMMIT})
    )

    status = generation.read_status()

    assert status.ending == "committed"
    assert status.recorded
    assert status.pid is None


def test_reports_a_run_whose_process_is_gone_as_unfinished(tmp_path: Path) -> None:
    generation = write_journal(tmp_path, write_header(), write_row(datetime.now(UTC).isoformat()))

    status = generation.read_status()

    assert status.recorded
    assert not status.ending
    assert status.pid is None
    assert status.started is None


# A halt leaves the stop file of the run it ended behind. That file beside a process that is gone asks for
# nothing, because no run is there to hear it.
def test_reports_no_stop_beside_a_run_whose_process_is_gone(tmp_path: Path) -> None:
    generation = write_journal(tmp_path, write_header(), json.dumps({"kind": "conclusion", "ending": "stopped"}))
    generation.cancel_file.touch()

    status = generation.read_status()

    assert not status.stopping
    assert status.pid is None
    assert status.ending == "stopped"


def test_reports_the_draft_a_run_saved(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()
    generation.workspace.draft_file.write_bytes(DRAFT)

    status = generation.read_status()

    assert status.draft
    assert not status.recorded


def test_reports_the_window_that_has_the_project(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)

    with hold_workspace(tmp_path) as window:
        status = generation.read_status()

    assert status.holder == window.pid


# A report is a pure read. A project with no run stays a project with no run, and the directory of a run that
# never ran must not appear.
def test_writes_nothing_into_a_project_with_no_run(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)

    status = generation.read_status()

    assert status.pid is None
    assert status.holder is None
    assert not status.recorded
    assert not status.draft
    assert not generation.workspace.generation_dir.exists()


# A killed run leaves the line it was writing without its end. A report says what the lines before it hold.
def test_reports_what_a_journal_with_a_partial_last_line_holds(tmp_path: Path) -> None:
    generation = write_journal(
        tmp_path, write_header(), json.dumps({"kind": "conclusion", "ending": "committed", "commit": COMMIT})
    )
    with generation.journal_file.open("ab") as journal:
        journal.write(b'{"kind": "thou')

    status = generation.read_status()

    assert status.ending == "committed"


def test_keeps_running_when_the_process_that_started_it_dies(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    install_workspace(tmp_path)
    (tmp_path / paths.SETTINGS_FILE).write_text(SETTINGS, encoding="utf-8")
    monkeypatch.setenv("JRI_TEST_API_KEY", "unused")
    # Detach HEAD. This is the one test that runs the real generation
    # workflow, not a double, so make it fail fast and offline on a git
    # precondition rather than a network call this test has no credentials for.
    run_git(tmp_path, "checkout", "--detach", "-q")
    generation = Generation(Workspace(tmp_path))
    ready = tmp_path.parent / "started"
    starter = subprocess.Popen([sys.executable, "-c", STARTER, str(tmp_path), str(ready)])
    try:
        _watch_the_window_start_the_run(generation, starter)
    finally:
        starter.kill()
        starter.wait()

    with pytest.raises(RepositoryStateError, match="not on a branch"):
        list(generation.follow())


# Confirm the runner left the starter's session, not just its process
# group. A signal that reaches the starter's session, such as a
# terminal hangup, could otherwise still end the runner too.
#
# Read the header the moment the runner writes it, rather than waiting
# for the window to report the run started. A session belongs to a
# process only while that process runs, and the run below fails fast on
# the detached HEAD, so the window's report can arrive after the runner
# it names is already gone.
def _watch_the_window_start_the_run(generation: Generation, starter: "subprocess.Popen[bytes]") -> None:
    deadline = time.monotonic() + STARTS_WITHIN
    written = b""
    while b"\n" not in written:
        assert time.monotonic() < deadline, "the window never started the run"
        written = generation.journal_file.read_bytes() if generation.journal_file.exists() else b""
        if b"\n" not in written:
            time.sleep(POLL)
    if sys.platform == "win32":
        return
    header = json.loads(written.partition(b"\n")[0])
    assert os.getsid(header["pid"]) != os.getsid(starter.pid)
