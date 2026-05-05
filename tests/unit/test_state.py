from pathlib import Path

import pytest

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


def test_load_recovers_primary_from_valid_backup_and_repairs_primary_file(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    expected = State(finished_at=123, session="ses_123")

    store.save(expected)
    store.path.write_text('{"invalid": json}', encoding="utf-8")

    recovered = store.load()

    assert recovered == expected
    assert store.path.read_text(encoding="utf-8") == store.backup_path.read_text(
        encoding="utf-8"
    )


def test_save_interruption_preserves_previous_readable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    original = State(session="ses_original")
    updated = State(session="ses_updated")
    store.save(original)
    write_calls: list[Path] = []
    original_write = store._write_text_atomically

    def interrupted_write(path: Path, text: str) -> None:
        write_calls.append(path)
        if path == store.path:
            temp_path = store._temp_path_for(path)
            temp_path.write_text(text[:8], encoding="utf-8")
            raise OSError("simulated crash before replace")
        original_write(path, text)

    monkeypatch.setattr(store, "_write_text_atomically", interrupted_write)

    with pytest.raises(OSError, match="simulated crash"):
        store.save(updated)

    assert write_calls == [store.path]
    assert store.load() == original


def test_save_keeps_new_primary_state_when_backup_refresh_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    original = State(session="ses_original")
    updated = State(session="ses_updated")
    store.save(original)
    original_write = store._write_text_atomically

    def interrupted_backup_write(path: Path, text: str) -> None:
        if path == store.backup_path:
            temp_path = store._temp_path_for(path)
            temp_path.write_text(text[:8], encoding="utf-8")
            raise OSError("simulated crash refreshing backup")
        original_write(path, text)

    monkeypatch.setattr(store, "_write_text_atomically", interrupted_backup_write)

    with pytest.raises(OSError, match="refreshing backup"):
        store.save(updated)

    assert store.load() == updated


def test_save_writes_backup_copy(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    state = State(branch="main")

    store.save(state)

    assert store.backup_path.exists()
    assert store.path.read_text(encoding="utf-8") == store.backup_path.read_text(
        encoding="utf-8"
    )


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

    assert tuple(TASK_STATUSES) == ("draft", "todo", "doing", "done")
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
