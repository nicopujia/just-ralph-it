from pathlib import Path

from jri.core.timeline import TimelineEvent, TimelineStore


def test_timeline_event_to_jsonl_includes_required_fields(
    tmp_path: Path,
) -> None:
    event = TimelineEvent(ts="2025-01-01T00:00:00Z", event="attempt_started")
    line = event.to_jsonl()
    assert '"event"' in line
    assert "attempt_started" in line
    assert '"ts"' in line
    assert "2025-01-01T00:00:00Z" in line


def test_timeline_event_to_jsonl_omits_none_fields(tmp_path: Path) -> None:
    event = TimelineEvent(ts="2025-01-01T00:00:00Z", event="attempt_started")
    line = event.to_jsonl()
    assert '"task"' not in line
    assert '"detail"' not in line


def test_timeline_event_to_jsonl_includes_optional_fields(tmp_path: Path) -> None:
    event = TimelineEvent(
        ts="2025-01-01T00:00:00Z",
        event="task_failed",
        task="my-task",
        detail={"reason": "make_check"},
    )
    line = event.to_jsonl()
    payload = __import__("json").loads(line)
    assert payload["task"] == "my-task"
    assert payload["detail"]["reason"] == "make_check"


def test_timeline_store_record_and_read(tmp_path: Path) -> None:
    path = tmp_path / "timeline.jsonl"
    store = TimelineStore(path)

    store.record(
        TimelineEvent(
            ts="2025-01-01T00:00:00Z",
            event="attempt_started",
            task="task-a",
        )
    )
    store.record(
        TimelineEvent(
            ts="2025-01-01T00:01:00Z",
            event="task_completed",
            task="task-a",
        )
    )

    events = store.read()
    assert len(events) == 2
    assert events[0].event == "attempt_started"
    assert events[0].task == "task-a"
    assert events[1].event == "task_completed"


def test_timeline_store_read_empty(tmp_path: Path) -> None:
    path = tmp_path / "nonexistent.jsonl"
    store = TimelineStore(path)
    assert store.read() == []


def test_timeline_store_read_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "timeline.jsonl"
    path.write_text("bad line\n{not json\n", encoding="utf-8")
    store = TimelineStore(path)
    assert store.read() == []


def test_timeline_store_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "timeline.jsonl"
    store = TimelineStore(path)
    store.record(TimelineEvent(ts="2025-01-01T00:00:00Z", event="attempt_started"))
    assert path.exists()


def test_timeline_store_now_iso_format(tmp_path: Path) -> None:
    ts = TimelineStore.now_iso()
    assert ts.endswith("Z")
    assert "T" in ts
