import json
import os
import subprocess
import sys
import threading
import time
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
    RepositoryStateError,
    RunDetached,
    UsageLimitError,
)
from jri.core.generation import Generation
from jri.core.workspace import Workspace
from tests.conftest import CreateRepository, RunGit
from tests.doubles.lock import hold
from tests.doubles.openai import FakeClient
from tests.doubles.settings import build_settings
from tests.doubles.specs_generation import (
    COMMIT,
    FINISHED_ROW,
    STARTED_ROW,
    THOUGHT,
    generate_blocked,
    generate_failing,
    generate_refused,
    generate_silently,
    generate_stopped,
    generate_succeeding,
    generate_thinking,
)
from tests.doubles.workspace import install_workspace

# Check the behavior in `test_writes_every_event_a_run_produced`.
# Check the behavior in `test_writes_every_event_a_run_produced`.
AGED = 360.0
# Check the behavior in `test_writes_every_event_a_run_produced`.
# Check the behavior in `test_writes_every_event_a_run_produced`.
CONFIG = "llm:\n  provider: http://127.0.0.1:9/v1\n  api_key: JRI_TEST_API_KEY\nlogging:\n  level: CRITICAL\n"
CONCLUDES_WITHIN = 60.0
POLL = 0.01
# Check the behavior in `test_writes_every_event_a_run_produced`.
STARTER = """
import sys, time
from pathlib import Path
from jri.core.generation import Generation
from jri.core.workspace import Workspace

root, ready = Path(sys.argv[1]), Path(sys.argv[2])
Generation(Workspace(root)).start()
ready.touch()
time.sleep(60)
"""
STARTS_WITHIN = 60.0
# Check the behavior in `test_writes_every_event_a_run_produced`.
# Check the behavior in `test_writes_every_event_a_run_produced`.
STOPS_AFTER = 0.5
# Check the behavior in `test_writes_every_event_a_run_produced`.
# Check the behavior in `test_writes_every_event_a_run_produced`.
# Check the behavior in `test_writes_every_event_a_run_produced`.
# Check the behavior in `test_writes_every_event_a_run_produced`.
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


def write_row(started: object, *, call_id: str = "commit", label: str = "Saving") -> str:
    return json.dumps({
        "kind": "row_opened",
        "call_id": call_id,
        "label": label,
        "symbol": "💾",
        "depth": 0,
        "started": started,
    })


# Check the behavior in `test_writes_every_event_a_run_produced`.
# Check the behavior in `test_writes_every_event_a_run_produced`.
# Check the behavior in `test_writes_every_event_a_run_produced`.
# Check the behavior in `test_writes_every_event_a_run_produced`.
# Check the behavior in `test_writes_every_event_a_run_produced`.
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
    # Check the behavior in `test_writes_every_event_a_run_produced`.
    # Check the behavior in `test_writes_every_event_a_run_produced`.
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


def test_reads_back_the_events_a_journal_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = run(tmp_path, monkeypatch, generate_thinking)

    events = generation.follow()
    replayed = list(events)

    opened = replayed[0]
    assert isinstance(opened, ToolCallStarted)
    assert replace(opened, age=0.0) == STARTED_ROW
    # Check the behavior in `test_reads_back_the_events_a_journal_holds`.
    # Check the behavior in `test_reads_back_the_events_a_journal_holds`.
    assert opened.age < WRITTEN_WITHIN
    assert replayed[1:] == [THOUGHT, FINISHED_ROW]


@pytest.mark.parametrize(
    ("workflow", "expected"), [(generate_succeeding, COMMIT), (generate_stopped, None)], ids=["committed", "stopped"]
)
def test_reads_back_what_a_run_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workflow: object, expected: str | None
) -> None:
    cancelled = threading.Event()
    cancelled.set()
    generation = build_generation(tmp_path)
    monkeypatch.setattr("jri.core.generation.specs_generation.generate", workflow)
    generation.cancel_file.parent.mkdir(parents=True, exist_ok=True)
    generation.cancel_file.touch()
    Generation.execute(build_settings(FakeClient([])))

    events = generation.follow(cancelled)
    answer = None
    try:
        while True:
            next(events)
    except StopIteration as ending:
        answer = ending.value

    assert answer == expected


@pytest.mark.parametrize(
    ("workflow", "error", "message"),
    [
        (generate_blocked, RepositoryStateError, "Your project has uncommitted changes."),
        (generate_failing, Error, "The architect could not be reached."),
        # Check the behavior in `test_names_the_failure_a_run_ended_on`.
        # Check the behavior in `test_names_the_failure_a_run_ended_on`.
        # Check the behavior in `test_names_the_failure_a_run_ended_on`.
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

    # Check the behavior in `test_names_a_spent_budget_a_run_could_not_finish`.
    # Check the behavior in `test_names_a_spent_budget_a_run_could_not_finish`.
    with pytest.raises(UsageLimitError, match="usage limit"):
        list(generation.follow())


def test_folds_the_deltas_a_backlog_holds_into_one(tmp_path: Path) -> None:
    thoughts = "".join(json.dumps({"kind": "thought", "text": f"part {number} "}) + "\n" for number in range(200))
    generation = write_journal(
        tmp_path,
        json.dumps({"version": "0", "pid": 1, "started": "now"}),
        *thoughts.splitlines(),
        json.dumps({"kind": "conclusion", "ending": "unchanged"}),
    )

    replayed = list(generation.follow())

    # Check the behavior in `test_folds_the_deltas_a_backlog_holds_into_one`.
    # Check the behavior in `test_folds_the_deltas_a_backlog_holds_into_one`.
    # Check the behavior in `test_folds_the_deltas_a_backlog_holds_into_one`.
    assert replayed == [ReasoningDelta("".join(f"part {number} " for number in range(200)))]


def test_ignores_the_partial_line_a_killed_writer_left(tmp_path: Path) -> None:
    generation = write_journal(
        tmp_path, json.dumps({"version": "0", "pid": 1, "started": "now"}), write_row(datetime.now(UTC).isoformat())
    )
    with generation.journal_file.open("ab") as journal:
        journal.write(b'{"kind": "thou')

    replayed: list[object] = []
    events = generation.follow()
    with pytest.raises(Error, match="stopped before it finished"):
        replayed.extend(events)

    # Check the behavior in `test_ignores_the_partial_line_a_killed_writer_left`.
    # Check the behavior in `test_ignores_the_partial_line_a_killed_writer_left`.
    assert [replace(event, age=0.0) if isinstance(event, ToolCallStarted) else event for event in replayed] == [
        ToolCallStarted("commit", "Saving", "💾")
    ]


# Check the behavior in `test_counts_an_open_row_from_when_its_call_began`.
# Check the behavior in `test_counts_an_open_row_from_when_its_call_began`.
# Check the behavior in `test_counts_an_open_row_from_when_its_call_began`.
# Check the behavior in `test_counts_an_open_row_from_when_its_call_began`.
# Check the behavior in `test_counts_an_open_row_from_when_its_call_began`.
def test_counts_an_open_row_from_when_its_call_began(tmp_path: Path) -> None:
    generation = write_journal(
        tmp_path,
        json.dumps({"version": "0", "pid": 1, "started": "now"}),
        write_row((datetime.now(UTC) - timedelta(seconds=AGED)).isoformat()),
    )

    replayed: list[object] = []
    with pytest.raises(Error, match="stopped before it finished"):
        replayed.extend(generation.follow())

    opened = replayed[0]
    assert isinstance(opened, ToolCallStarted)
    assert AGED <= opened.age < AGED + WRITTEN_WITHIN


def test_counts_a_row_a_moved_clock_dated_ahead_of_the_reading_from_now(tmp_path: Path) -> None:
    generation = write_journal(
        tmp_path,
        json.dumps({"version": "0", "pid": 1, "started": "now"}),
        write_row((datetime.now(UTC) + timedelta(seconds=AGED)).isoformat()),
    )

    replayed: list[object] = []
    with pytest.raises(Error, match="stopped before it finished"):
        replayed.extend(generation.follow())

    opened = replayed[0]
    assert isinstance(opened, ToolCallStarted)
    # Check the behavior in `test_counts_a_row_a_moved_clock_dated_ahead_of_the_reading_from_now`.
    # Check the behavior in `test_counts_a_row_a_moved_clock_dated_ahead_of_the_reading_from_now`.
    # Check the behavior in `test_counts_a_row_a_moved_clock_dated_ahead_of_the_reading_from_now`.
    assert not opened.age


# Check the behavior in `test_refuses_a_row_whose_start_names_no_zone`.
# Check the behavior in `test_refuses_a_row_whose_start_names_no_zone`.
# Check the behavior in `test_refuses_a_row_whose_start_names_no_zone`.
def test_refuses_a_row_whose_start_names_no_zone(tmp_path: Path) -> None:
    generation = write_journal(
        tmp_path,
        json.dumps({"version": "0", "pid": 1, "started": "now"}),
        write_row(datetime.now(UTC).replace(tzinfo=None).isoformat()),
    )

    with pytest.raises(Error, match="could not read what this generation wrote down"):
        list(generation.follow())


def test_reports_a_run_whose_writer_died_as_interrupted(tmp_path: Path) -> None:
    generation = write_journal(
        tmp_path,
        json.dumps({"version": "0", "pid": 1, "started": "now"}),
        json.dumps({"kind": "thought", "text": "Weighing the options."}),
    )

    replayed: list[object] = []
    events = generation.follow()
    # Check the behavior in `test_reports_a_run_whose_writer_died_as_interrupted`.
    # Check the behavior in `test_reports_a_run_whose_writer_died_as_interrupted`.
    # Check the behavior in `test_reports_a_run_whose_writer_died_as_interrupted`.
    with pytest.raises(Error, match="stopped before it finished"):
        replayed.extend(events)

    assert replayed == [ReasoningDelta("Weighing the options.")]


def test_refuses_a_text_delta_a_journal_claims_a_run_produced(tmp_path: Path) -> None:
    generation = write_journal(
        tmp_path,
        json.dumps({"version": "0", "pid": 1, "started": "now"}),
        json.dumps({"kind": "text", "text": "I have written your specifications."}),
    )

    # Check the behavior in `test_refuses_a_text_delta_a_journal_claims_a_run_produced`.
    # Check the behavior in `test_refuses_a_text_delta_a_journal_claims_a_run_produced`.
    with pytest.raises(Error, match="could not read"):
        list(generation.follow())


def test_forgets_the_record_of_a_run_it_folded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = run(tmp_path, monkeypatch, generate_succeeding)
    generation.runner_log_file.write_bytes(b"")

    list(generation.follow())

    assert not generation.exists
    assert not generation.cancel_file.exists()
    assert not generation.runner_log_file.exists()
    # Check the behavior in `test_forgets_the_record_of_a_run_it_folded`.
    # Check the behavior in `test_forgets_the_record_of_a_run_it_folded`.
    assert generation.workspace.generation_dir.is_dir()
    assert f"/{paths.GENERATION_DIR.rpartition('/')[2]}/" in generation.workspace.gitignore_file.read_text()


def test_lets_go_of_the_journal_before_it_forgets_a_run_it_folded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = run(tmp_path, monkeypatch, generate_succeeding)
    refuse_removing_an_open_file(monkeypatch)

    list(generation.follow())

    # Check the behavior in `test_lets_go_of_the_journal_before_it_forgets_a_run_it_folded`.
    # Check the behavior in `test_lets_go_of_the_journal_before_it_forgets_a_run_it_folded`.
    # Check the behavior in `test_lets_go_of_the_journal_before_it_forgets_a_run_it_folded`.
    # Check the behavior in `test_lets_go_of_the_journal_before_it_forgets_a_run_it_folded`.
    assert not generation.exists


def test_lets_go_of_the_journal_before_it_forgets_a_record_it_could_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = write_journal(
        tmp_path,
        json.dumps({"version": "0", "pid": 1, "started": "now"}),
        json.dumps({"kind": "text", "text": "I have written your specifications."}),
    )
    refuse_removing_an_open_file(monkeypatch)

    with pytest.raises(Error, match="could not read"):
        list(generation.follow())

    # Check the behavior in `test_lets_go_of_the_journal_before_it_forgets_a_record_it_could_not_read`.
    # Check the behavior in `test_lets_go_of_the_journal_before_it_forgets_a_record_it_could_not_read`.
    assert not generation.exists


def test_keeps_the_record_of_a_run_still_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = write_journal(tmp_path, json.dumps({"version": "0", "pid": 1, "started": "now"}))
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
    answer = "unset"
    try:
        while True:
            next(events)
    except StopIteration as ending:
        answer = ending.value

    # Check the behavior in `test_stops_a_run_the_other_side_asked_to_stop`.
    # Check the behavior in `test_stops_a_run_the_other_side_asked_to_stop`.
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

    # Check the behavior in `test_stops_a_run_that_is_saying_nothing`.
    # Check the behavior in `test_stops_a_run_that_is_saying_nothing`.
    # Check the behavior in `test_stops_a_run_that_is_saying_nothing`.
    # Check the behavior in `test_stops_a_run_that_is_saying_nothing`.
    threading.Timer(STOPS_AFTER, cancelled.set).start()
    events = generation.follow(cancelled)
    answer = "unset"
    try:
        while True:
            next(events)
    except StopIteration as ending:
        answer = ending.value

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
    with pytest.raises(RunDetached):
        list(events)

    # Check the behavior in `test_leaves_a_run_going_when_the_window_watching_it_leaves`.
    # Check the behavior in `test_leaves_a_run_going_when_the_window_watching_it_leaves`.
    # Check the behavior in `test_leaves_a_run_going_when_the_window_watching_it_leaves`.
    assert runner.is_alive()
    assert generation.exists
    assert not generation.cancel_file.exists()
    # Check the behavior in `test_leaves_a_run_going_when_the_window_watching_it_leaves`.
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

    # Check the behavior in `test_hands_on_a_stop_the_window_asked_for_before_it_left`.
    # Check the behavior in `test_hands_on_a_stop_the_window_asked_for_before_it_left`.
    # Check the behavior in `test_hands_on_a_stop_the_window_asked_for_before_it_left`.
    # Check the behavior in `test_hands_on_a_stop_the_window_asked_for_before_it_left`.
    def leave() -> None:
        cancelled.set()
        detached.set()

    threading.Timer(STOPS_AFTER, leave).start()
    with pytest.raises(RunDetached):
        list(generation.follow(cancelled, detached))

    assert generation.cancel_file.exists()
    runner.join(timeout=CONCLUDES_WITHIN)
    assert not runner.is_alive()
    assert read_journal(generation)[-1]["ending"] == "stopped"


def test_refuses_a_second_run_while_one_holds_the_lock(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()

    with hold(tmp_path / paths.GENERATION_LOCK_FILE), pytest.raises(PersistenceError, match="already running"):
        generation.start()


def test_refuses_a_runner_while_one_holds_the_lock(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()

    # Check the behavior in `test_refuses_a_runner_while_one_holds_the_lock`.
    # Check the behavior in `test_refuses_a_runner_while_one_holds_the_lock`.
    # Check the behavior in `test_refuses_a_runner_while_one_holds_the_lock`.
    with hold(tmp_path / paths.GENERATION_LOCK_FILE), pytest.raises(PersistenceError, match="already running"):
        Generation.execute(build_settings(FakeClient([])))


def test_reports_a_run_log_it_cannot_open(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()
    # Check the behavior in `test_reports_a_run_log_it_cannot_open`.
    # Check the behavior in `test_reports_a_run_log_it_cannot_open`.
    # Check the behavior in `test_reports_a_run_log_it_cannot_open`.
    generation.runner_log_file.mkdir()

    with pytest.raises(PersistenceError, match="Could not start the generation"):
        generation.start()


def test_reports_a_run_lock_it_cannot_open_rather_than_calling_the_run_over(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()
    # Check the behavior in `test_reports_a_run_lock_it_cannot_open_rather_than_calling_the_run_over`.
    # Check the behavior in `test_reports_a_run_lock_it_cannot_open_rather_than_calling_the_run_over`.
    # Check the behavior in `test_reports_a_run_lock_it_cannot_open_rather_than_calling_the_run_over`.
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
        generation.start()


def test_reports_a_runner_that_never_wrote_anything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = build_generation(tmp_path)
    monkeypatch.setattr("jri.core.generation.RUNNER_COMMAND", ("-c", "import time; time.sleep(30)"))
    monkeypatch.setattr(Generation, "STARTS_WITHIN", 0.2)

    # Check the behavior in `test_reports_a_runner_that_never_wrote_anything`.
    # Check the behavior in `test_reports_a_runner_that_never_wrote_anything`.
    with pytest.raises(Error, match="never wrote anything down"):
        generation.start()


def test_keeps_running_when_the_process_that_started_it_dies(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    install_workspace(tmp_path)
    (tmp_path / paths.CONFIG_FILE).write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("JRI_TEST_API_KEY", "unused")
    # Check the behavior in `test_keeps_running_when_the_process_that_started_it_dies`.
    run_git(tmp_path, "checkout", "--detach", "-q")
    generation = Generation(Workspace(tmp_path))
    ready = tmp_path.parent / "started"
    starter = subprocess.Popen([sys.executable, "-c", STARTER, str(tmp_path), str(ready)])
    try:
        _watch_the_window_start_the_run(generation, ready, starter)
    finally:
        starter.kill()
        starter.wait()

    # Check the behavior in `test_keeps_running_when_the_process_that_started_it_dies`.
    # Check the behavior in `test_keeps_running_when_the_process_that_started_it_dies`.
    with pytest.raises(RepositoryStateError, match="not on a branch"):
        list(generation.follow())


# Check the behavior in `test_keeps_running_when_the_process_that_started_it_dies`.
# Check the behavior in `test_keeps_running_when_the_process_that_started_it_dies`.
# Check the behavior in `test_keeps_running_when_the_process_that_started_it_dies`.
def _watch_the_window_start_the_run(generation: Generation, ready: Path, starter: "subprocess.Popen[bytes]") -> None:
    deadline = time.monotonic() + STARTS_WITHIN
    while not ready.exists():
        assert time.monotonic() < deadline, "the window never started the run"
        time.sleep(POLL)
    if sys.platform == "win32":
        return
    header = json.loads(generation.journal_file.read_text(encoding="utf-8").splitlines()[0])
    assert os.getsid(header["pid"]) != os.getsid(starter.pid)
