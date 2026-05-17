from pathlib import Path
from typing import cast

import pytest

from jri.core.metrics import MetricEntry, MetricsStore


def test_metric_entry_to_dict_contains_all_fields() -> None:
    entry = MetricEntry(task="some-slug", ts="2026-04-05T14:30:00Z", result="pass")
    d = entry.to_dict()
    assert d == {"task": "some-slug", "ts": "2026-04-05T14:30:00Z", "result": "pass"}


def test_metric_entry_result_can_be_pass_or_fail() -> None:
    entry_pass = MetricEntry(task="a", ts="t", result="pass")
    entry_fail = MetricEntry(task="b", ts="t", result="fail")
    assert entry_pass.result == "pass"
    assert entry_fail.result == "fail"


def test_metrics_store_record_and_read(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    store = MetricsStore(path)

    store.record(MetricEntry(task="task-a", ts="2026-04-05T10:00:00Z", result="pass"))
    store.record(MetricEntry(task="task-b", ts="2026-04-05T11:00:00Z", result="fail"))

    entries = store.read()
    assert len(entries) == 2
    assert entries[0].task == "task-a"
    assert entries[0].result == "pass"
    assert entries[1].task == "task-b"
    assert entries[1].result == "fail"


def test_metrics_store_read_empty(tmp_path: Path) -> None:
    path = tmp_path / "nonexistent.json"
    store = MetricsStore(path)
    assert store.read() == []


def test_metrics_store_read_skips_malformed(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    path.write_text("not json\n", encoding="utf-8")
    store = MetricsStore(path)
    assert store.read() == []


def test_metrics_store_read_skips_non_list_payload(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    path.write_text("{}\n", encoding="utf-8")
    store = MetricsStore(path)
    assert store.read() == []


def test_metrics_store_read_skips_entries_with_wrong_types(tmp_path: Path) -> None:
    import json

    path = tmp_path / "metrics.json"
    payload = [{"task": 2, "ts": "t", "result": "pass"}, {"task": "a", "ts": "t", "result": "unknown"}]
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    store = MetricsStore(path)
    assert store.read() == []


def test_metrics_store_read_skips_non_dict_entries(tmp_path: Path) -> None:
    import json

    path = tmp_path / "metrics.json"
    path.write_text(json.dumps([1, {"task": "a", "ts": "t", "result": "pass"}]), encoding="utf-8")
    store = MetricsStore(path)

    entries = store.read()

    assert len(entries) == 1
    assert entries[0].task == "a"


def test_metrics_store_record_reports_write_failures(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "metrics.json"
    store = MetricsStore(path)

    def fail_read(_self: MetricsStore) -> list[MetricEntry]:
        raise OSError("boom")

    monkeypatch.setattr(MetricsStore, "read", fail_read)

    store.record(MetricEntry(task="a", ts="t", result="pass"))

    assert "metrics write failed: boom" in capsys.readouterr().err


def test_metrics_store_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "metrics.json"
    store = MetricsStore(path)
    store.record(MetricEntry(task="a", ts="t", result="pass"))
    assert path.exists()


def test_metrics_store_survives_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    # Write a valid entry
    store = MetricsStore(path)
    store.record(MetricEntry(task="a", ts="t", result="pass"))
    # Corrupt the file
    path.write_text("corrupted{", encoding="utf-8")
    # Reading should return empty without crashing
    assert store.read() == []


def test_metrics_store_appends_not_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    store = MetricsStore(path)

    store.record(MetricEntry(task="a", ts="t1", result="pass"))
    store.record(MetricEntry(task="b", ts="t2", result="fail"))
    store.record(MetricEntry(task="c", ts="t3", result="pass"))

    entries = store.read()
    assert len(entries) == 3
    assert entries[0].task == "a"
    assert entries[1].task == "b"
    assert entries[2].task == "c"


def test_metrics_store_summary_no_entries(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    store = MetricsStore(path)
    assert store.summary() is None


def test_metrics_store_summary_all_pass(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    store = MetricsStore(path)
    for i in range(5):
        store.record(MetricEntry(task=f"task-{i}", ts="t", result="pass"))
    s = store.summary()
    assert s is not None
    assert "5 runs" in s
    assert "5 pass" in s
    assert "0 fail" in s
    assert "100% pass rate" in s


def test_metrics_store_summary_mixed(tmp_path: Path) -> None:
    path = tmp_path / "metrics.json"
    store = MetricsStore(path)
    store.record(MetricEntry(task="a", ts="t", result="pass"))
    store.record(MetricEntry(task="b", ts="t", result="fail"))
    store.record(MetricEntry(task="c", ts="t", result="pass"))
    s = store.summary()
    assert s is not None
    assert "3 runs" in s
    assert "2 pass" in s
    assert "1 fail" in s
    assert "67% pass rate" in s


def test_metrics_store_now_iso_format() -> None:
    ts = MetricsStore.now_iso()
    assert ts.endswith("Z")
    assert "T" in ts


def test_metrics_store_file_is_valid_json_array(tmp_path: Path) -> None:
    import json

    path = tmp_path / "metrics.json"
    store = MetricsStore(path)
    store.record(MetricEntry(task="a", ts="2026-04-05T14:30:00Z", result="pass"))
    data = cast(list[dict[str, object]], json.loads(path.read_text(encoding="utf-8")))
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["task"] == "a"
    assert data[0]["result"] == "pass"
