from pathlib import Path

import pytest

from jri.core.models import AttemptState, State
from jri.core.state import StateStore


def test_load_restores_from_backup_when_primary_is_invalid(tmp_path: Path) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    expected = State(iteration_number=3, finished_at=123, session="ses_123")

    store.save(expected)
    store.path.write_text('{"iteration": ', encoding="utf-8")

    recovered = store.load()

    assert recovered == expected
    assert store.load() == expected


def test_save_interruption_preserves_previous_readable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path / ".jri" / "state.json")
    original = State(iteration_number=1, session="ses_original")
    updated = State(iteration_number=2, session="ses_updated")
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
    original = State(iteration_number=1, session="ses_original")
    updated = State(iteration_number=2, session="ses_updated")
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
    state = State(iteration_number=4, branch="main")

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
        iteration_number=3,
        branch="ralph/3/task-a",
        started_at=123,
        finished_at=456,
        log_path=".jri/logs/ralph/3.log",
        session_id="ses_123",
        outcome="interrupted",
    )
    expected = State(
        iteration_number=2,
        started_at=123,
        finished_at=456,
        session="ses_latest",
        branch="main",
        active_attempt=attempt,
        attempts=[attempt],
    )

    store.save(expected)

    assert store.load() == expected
