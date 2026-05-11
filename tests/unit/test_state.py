from pathlib import Path

import pytest

from jri.core.errors import JriError
from jri.core.models import (
    ATTEMPT_RESULT_VALUES,
    JRI_LIFECYCLE_INVARIANTS,
    TASK_STATUSES,
    AttemptState,
    RalphResultPayload,
    State,
)
from jri.core.state import StateStore


def test_load_restores_from_backup_when_primary_is_invalid(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    expected = State(finished_at=123, session="ses_123")

    store.save(expected)
    store.path.write_text('{"invalid": json}', encoding="utf-8")

    recovered = store.load()

    assert recovered == expected
    assert store.load() == expected


def test_load_returns_default_state_when_state_file_is_missing(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")

    assert store.load() == State()


def test_load_recovers_primary_from_valid_backup_and_repairs_primary_file(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    expected = State(finished_at=123, session="ses_123")

    store.save(expected)
    store.path.write_text('{"invalid": json}', encoding="utf-8")

    recovered = store.load()

    assert recovered == expected
    assert StateStore(store.path).load() == expected


def test_load_prefers_readable_primary_over_stale_backup(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    original = State(session="ses_original")
    updated = State(session="ses_updated")
    store.save(original)
    store.path.write_text('{"session": "ses_updated"}\n', encoding="utf-8")

    assert store.load() == updated


def test_clear_process_removes_saved_process_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")

    store.save_process(
        loop_pid=7,
        child_pid=8,
        log_path=tmp_path / "logs" / "loop.log",
        detached=True,
    )
    store.clear_process()

    assert store.load().process is None


def test_state_store_updates_process_session_and_attempt_fields(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    first_attempt = AttemptState(
        number=1,
        task_slug="task-a",
        branch="ralph",
        started_at=100,
        log_path=".jri/logs/ralph/task-a-100.log",
        session_id="ses_1",
        result="timeout",
    )
    updated_attempt = AttemptState(
        number=1,
        task_slug="task-a",
        branch="ralph",
        started_at=101,
        finished_at=111,
        log_path=".jri/logs/ralph/task-a-101.log",
        session_id="ses_2",
        result="failed",
    )
    second_attempt = AttemptState(
        number=2,
        task_slug="task-b",
        branch="ralph",
        started_at=200,
        log_path=".jri/logs/ralph/task-b-200.log",
        session_id="ses_3",
        result="timeout",
    )

    store.save_process(
        loop_pid=7,
        child_pid=8,
        log_path=tmp_path / "logs" / "loop.log",
        detached=True,
    )
    store.save_session("ses_state")
    store.start_attempt(first_attempt)
    store.save_active_attempt(updated_attempt)
    store.save_active_attempt(second_attempt)
    store.clear_active_attempt()
    store.mark_task_started(task_slug="task-a", started_at=123)
    store.mark_task_finished(task_slug="task-a", finished_at=456)

    state = store.load()

    assert state.process is not None
    assert state.process.loop_pid == 7
    assert state.process.child_pid == 8
    assert state.process.log_path == str(tmp_path / "logs" / "loop.log")
    assert state.process.detached is True
    assert state.session == "ses_state"
    assert state.active_attempt is None
    assert state.current_task is None
    assert state.started_at is None
    assert state.finished_at == 456
    assert [attempt.number for attempt in state.attempts] == [1, 2]
    assert state.attempts[0].started_at == 101
    assert state.attempts[0].finished_at == 111
    assert state.attempts[1] == second_attempt


def test_save_active_attempt_preserves_different_tasks_with_same_number(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    task_a_attempt = AttemptState(
        number=1,
        task_slug="task-a",
        branch="ralph/task-a",
        started_at=100,
        result="failed",
    )
    task_b_attempt = AttemptState(
        number=1,
        task_slug="task-b",
        branch="ralph/task-b",
        started_at=200,
        result="completed",
    )

    store.save_active_attempt(task_a_attempt)
    store.save_active_attempt(task_b_attempt)

    state = store.load()

    assert state.active_attempt == task_b_attempt
    assert state.attempts == [task_a_attempt, task_b_attempt]


def test_mark_task_finished_preserves_current_task_on_slug_mismatch(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    store.mark_task_started(task_slug="task-a", started_at=100)

    store.mark_task_finished(task_slug="task-b", finished_at=200)

    state = store.load()

    assert state.current_task == "task-a"
    assert state.started_at == 100
    assert state.finished_at is None


def test_load_uses_primary_when_backup_is_corrupted(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    original = State(session="ses_original")
    updated = State(session="ses_updated")
    store.save(original)
    store.save(updated)
    store.backup_path.write_text("[]\n", encoding="utf-8")

    assert store.load() == updated


def test_load_raises_primary_error_when_backup_is_missing(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(JriError, match="must contain an object"):
        store.load()


def test_load_rejects_invalid_state_payload(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"started_at": "soon"}\n', encoding="utf-8")

    with pytest.raises(JriError, match="invalid content"):
        store.load()


def test_load_raises_combined_error_when_backup_is_invalid(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("[]\n", encoding="utf-8")
    store.backup_path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(JriError, match="Backup recovery from state.json.bak failed"):
        store.load()


def test_load_recovers_primary_from_manually_seeded_backup(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    expected = State(session="ses_recovered")
    store.backup_path.parent.mkdir(parents=True, exist_ok=True)
    store.backup_path.write_text('{"session": "ses_recovered"}\n', encoding="utf-8")
    store.path.write_text("[]\n", encoding="utf-8")

    assert store.load() == expected
    assert StateStore(store.path).load() == expected


def test_saved_state_is_recoverable_when_primary_becomes_unreadable(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    state = State(branch="main")

    store.save(state)
    store.path.write_text("[]\n", encoding="utf-8")

    assert store.load() == state


def test_state_round_trips_attempt_metadata(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    attempt = AttemptState(
        number=2,
        task_slug="task-a",
        branch="ralph",
        started_at=123,
        finished_at=456,
        log_path=".jri/logs/ralph/task-a-123.log",
        session_id="ses_123",
        result="interrupted",
        result_payload=RalphResultPayload(
            result="incompleted",
            summary="Needs follow-up work.",
            learnings=["Capture the partial failure context."],
        ),
    )
    expected = State(
        started_at=123,
        finished_at=456,
        session="ses_latest",
        branch="main",
        active_attempt=attempt,
        attempts=[attempt],
    )

    store.save(expected)

    assert store.load() == expected


def test_state_round_trips_attempt_timeout_result(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    attempt = AttemptState(
        number=1,
        task_slug="task-a",
        branch="ralph",
        started_at=123,
        finished_at=456,
        log_path=".jri/logs/ralph/task-a-123.log",
        session_id="ses_123",
        result="timeout",
    )
    expected = State(active_attempt=attempt, attempts=[attempt])

    store.save(expected)

    assert store.load() == expected


def test_state_round_trips_current_task(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    expected = State(current_task="task-a")

    store.save(expected)

    assert store.load() == expected


def test_lifecycle_invariant_vocabulary_covers_state_surfaces() -> None:
    invariants = {
        invariant.surface: invariant for invariant in JRI_LIFECYCLE_INVARIANTS
    }

    assert tuple(TASK_STATUSES) == ("todo", "doing", "done")
    assert invariants["task_files"].vocabulary == TASK_STATUSES
    assert invariants["persisted_attempts"].vocabulary == ATTEMPT_RESULT_VALUES
    assert invariants["result_payload"].vocabulary == (
        "present",
        "missing",
        "invalid",
    )
    assert invariants["logs"].vocabulary == ("present", "missing", "recovered")
    assert invariants["human_blockers"].vocabulary == (
        "todo",
        "depends_on",
        "needs_human",
    )


def test_load_normalizes_legacy_incomplete_attempt_result(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        """
{
  "attempts": [
    {
      "number": 1,
      "task_slug": "task-a",
      "branch": "ralph",
      "started_at": 123,
      "result": "incomplete"
    }
  ],
  "active_attempt": {
    "number": 1,
    "task_slug": "task-a",
    "branch": "ralph",
    "started_at": 123,
    "result": "incomplete"
  }
}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    state = store.load()

    assert state.active_attempt is not None
    assert state.active_attempt.result == "incompleted"
    assert state.attempts[0].result == "incompleted"
