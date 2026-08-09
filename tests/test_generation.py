import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO

import pytest

from jri.core import paths
from jri.core.ai import ReasoningDelta, ToolCallStarted
from jri.core.exceptions import Error, PersistenceError, RepositoryStateError, RunDetached, UsageLimitError
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
    generate_silently,
    generate_stopped,
    generate_succeeding,
    generate_thinking,
)
from tests.doubles.workspace import install_workspace

# A provider nothing answers at, so a run that reached a model call
# would fail rather than quietly leaving the machine.
CONFIG = "llm:\n  provider: http://127.0.0.1:9/v1\n  api_key: JRI_TEST_API_KEY\nlogging:\n  level: CRITICAL\n"
CONCLUDES_WITHIN = 60.0
POLL = 0.01
# A window that starts a run and is killed before the run can finish.
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
# Long enough that the reader has taken in everything the journal held
# when it opened, so what the stop lands in is a silent run.
STOPS_AFTER = 0.5


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


# Windows refuses to remove a file any process still has open, and a
# suite run on Linux would never find that out. What stands in for the
# platform is this process's own handles: the files opened here are the
# real ones the code under test opened, and the refusal is raised
# against exactly those it has not closed yet.
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

    assert replayed == [STARTED_ROW, THOUGHT, FINISHED_ROW]


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
    ],
    ids=["blocked", "failed"],
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

    # A budget the provider refused is not a crash, so it has to come
    # back out of the journal as the class the turn reads it by.
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

    # Attaching forty minutes in replays a handful of events rather
    # than every delta the run streamed, exactly as the recording folds
    # them while a run is watched.
    assert replayed == [ReasoningDelta("".join(f"part {number} " for number in range(200)))]


def test_ignores_the_partial_line_a_killed_writer_left(tmp_path: Path) -> None:
    generation = write_journal(
        tmp_path,
        json.dumps({"version": "0", "pid": 1, "started": "now"}),
        json.dumps({"kind": "row_opened", "call_id": "commit", "label": "Saving", "symbol": "💾", "depth": 0}),
    )
    with generation.journal_file.open("ab") as journal:
        journal.write(b'{"kind": "thou')

    replayed: list[object] = []
    events = generation.follow()
    with pytest.raises(Error, match="stopped before it finished"):
        replayed.extend(events)

    # The whole lines before the kill still reached the screen, and
    # the one the kill cut in half stopped nothing.
    assert replayed == [ToolCallStarted("commit", "Saving", "💾")]


def test_reports_a_run_whose_writer_died_as_interrupted(tmp_path: Path) -> None:
    generation = write_journal(
        tmp_path,
        json.dumps({"version": "0", "pid": 1, "started": "now"}),
        json.dumps({"kind": "thought", "text": "Weighing the options."}),
    )

    replayed: list[object] = []
    events = generation.follow()
    # The lock is free and no ending was written, so the operating
    # system has already answered that the writer is gone -- and what
    # the run had said by then is still said.
    with pytest.raises(Error, match="stopped before it finished"):
        replayed.extend(events)

    assert replayed == [ReasoningDelta("Weighing the options.")]


def test_refuses_a_text_delta_a_journal_claims_a_run_produced(tmp_path: Path) -> None:
    generation = write_journal(
        tmp_path,
        json.dumps({"version": "0", "pid": 1, "started": "now"}),
        json.dumps({"kind": "text", "text": "I have written your specifications."}),
    )

    # A run's own voice never reaches the user as the interviewer's,
    # and the journal is where that refusal has to hold again.
    with pytest.raises(Error, match="could not read"):
        list(generation.follow())


def test_forgets_the_record_of_a_run_it_folded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    generation = run(tmp_path, monkeypatch, generate_succeeding)
    generation.runner_log_file.write_bytes(b"")

    list(generation.follow())

    assert not generation.exists
    assert not generation.cancel_file.exists()
    assert not generation.runner_log_file.exists()
    # The directory outlives every run in it, and so does the rule
    # that keeps it out of the project.
    assert generation.workspace.generation_dir.is_dir()
    assert f"/{paths.GENERATION_DIR.rpartition('/')[2]}/" in generation.workspace.gitignore_file.read_text()


def test_lets_go_of_the_journal_before_it_forgets_a_run_it_folded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = run(tmp_path, monkeypatch, generate_succeeding)
    refuse_removing_an_open_file(monkeypatch)

    list(generation.follow())

    # The reader is suspended inside the journal when the ending it just
    # handed on is folded, and a platform that refuses to remove a file
    # this process holds open would leave the record of a finished run
    # for every Ralph after it to attach to instead of starting.
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

    # A journal the refusal left behind meets every run after this one
    # with the same refusal, and it is the reader's own file to let go.
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

    # The canceller and the run are in different processes, so the only
    # thing that ends the run is the run reading that it was asked to.
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

    # The stop arrives while the run is in the middle of a model call,
    # which is where a run spends nearly all of its time: a reader that
    # came back only when the run wrote something would hold the stop
    # for as long as that call lasted.
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

    # The window went and the run stayed: nothing of the run's was
    # folded away, nothing was asked to stop, and the record the next
    # window reads is where the run left it.
    assert runner.is_alive()
    assert generation.exists
    assert not generation.cancel_file.exists()
    # And the run is still there to be stopped by whoever comes back.
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

    # A window closed in the same breath as the stop it was asked for,
    # in the middle of the model call a run spends its time in. The run
    # is in another process, so a stop nothing wrote down never reaches
    # it, and the window is not there to be asked again.
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

    # The lock is what one run to a project rests on, so the runner
    # asks for it before it writes a line rather than trusting whoever
    # started it to have asked.
    with hold(tmp_path / paths.GENERATION_LOCK_FILE), pytest.raises(PersistenceError, match="already running"):
        Generation.execute(build_settings(FakeClient([])))


def test_reports_a_run_directory_it_cannot_write_in(tmp_path: Path) -> None:
    generation = build_generation(tmp_path)
    generation.workspace.open_generation_dir()
    generation.workspace.generation_dir.chmod(0o500)

    try:
        with pytest.raises(PersistenceError, match="Could not start the generation"):
            generation.start()
    finally:
        generation.workspace.generation_dir.chmod(0o700)


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

    # A window that waited for a runner that never opened a journal
    # would sit there for as long as the runner did.
    with pytest.raises(Error, match="never wrote anything down"):
        generation.start()


def test_keeps_running_when_the_process_that_started_it_dies(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    install_workspace(tmp_path)
    (tmp_path / paths.CONFIG_FILE).write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("JRI_TEST_API_KEY", "unused")
    # Off a branch, so the run reaches its ending without a model call.
    run_git(tmp_path, "checkout", "--detach", "-q")
    generation = Generation(Workspace(tmp_path))
    ready = tmp_path.parent / "started"
    starter = subprocess.Popen([sys.executable, "-c", STARTER, str(tmp_path), str(ready)])
    try:
        _watch_the_window_start_the_run(generation, ready, starter)
    finally:
        starter.kill()
        starter.wait()

    # The window is gone and the run finishes anyway, saying so where
    # the next window will read it.
    with pytest.raises(RepositoryStateError, match="not on a branch"):
        list(generation.follow())


# The window is given until it says the run is going, and the run's own
# record is read before the window is killed: a runner in a session of
# its own is one no signal aimed at that window reaches.
def _watch_the_window_start_the_run(generation: Generation, ready: Path, starter: "subprocess.Popen[bytes]") -> None:
    deadline = time.monotonic() + STARTS_WITHIN
    while not ready.exists():
        assert time.monotonic() < deadline, "the window never started the run"
        time.sleep(POLL)
    if sys.platform == "win32":
        return
    header = json.loads(generation.journal_file.read_text(encoding="utf-8").splitlines()[0])
    assert os.getsid(header["pid"]) != os.getsid(starter.pid)
