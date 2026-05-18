import json
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .models import MetricEntry, MetricResult

if TYPE_CHECKING:
    from .state import StateStore


@dataclass
class MetricsStore:
    state_store: "StateStore"
    legacy_path: Path | None = None

    def record(self, entry: MetricEntry) -> None:
        """Append a metric entry to runtime state."""
        try:
            entries = self.read()
            entries.append(entry)
            state = self.state_store.load()
            self.state_store.save(replace(state, metrics=entries))
            if self.legacy_path is not None:
                self.legacy_path.unlink(missing_ok=True)
        except Exception as exc:
            print(f"metrics write failed: {exc}. Entry: {json.dumps(entry.to_dict())}", file=sys.stderr)

    def read(self) -> list[MetricEntry]:
        entries = list(self.state_store.load().metrics)
        legacy_entries = self._read_legacy()
        if legacy_entries:
            return [*legacy_entries, *entries]
        return entries

    def _read_legacy(self) -> list[MetricEntry]:
        if self.legacy_path is None or not self.legacy_path.exists():
            return []
        try:
            payload: object = json.loads(self.legacy_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(payload, list):
            return []
        entries: list[MetricEntry] = []
        for item in cast(list[object], payload):
            if not isinstance(item, dict):
                continue
            entry = cast(dict[str, object], item)
            task = entry.get("task")
            ts = entry.get("ts")
            result = _metric_result_or_none(entry.get("result"))
            if not (isinstance(task, str) and isinstance(ts, str) and result is not None):
                continue
            entries.append(MetricEntry(task=task, ts=ts, result=result))
        return entries

    def summary(self) -> str | None:
        """Return a human-readable summary string, or None if no metrics."""
        entries = self.read()
        if not entries:
            return None
        total = len(entries)
        passed = sum(1 for e in entries if e.result == "pass")
        failed = total - passed
        rate = round(passed / total * 100) if total > 0 else 0
        return f"metrics: {total} runs, {passed} pass, {failed} fail ({rate}% pass rate)"

    @staticmethod
    def now_iso() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _metric_result_or_none(value: object) -> MetricResult | None:
    if value == "pass":
        return "pass"
    if value == "fail":
        return "fail"
    return None
