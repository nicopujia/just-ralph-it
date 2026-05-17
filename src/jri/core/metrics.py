import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

MetricResult = Literal["pass", "fail"]


@dataclass(frozen=True)
class MetricEntry:
    task: str
    ts: str
    result: MetricResult

    def to_dict(self) -> dict[str, object]:
        return {"task": self.task, "ts": self.ts, "result": self.result}


@dataclass
class MetricsStore:
    path: Path

    def record(self, entry: MetricEntry) -> None:
        """Append a metric entry to the metrics file.

        Loads the existing array, appends the new entry, and saves atomically
        via a same-directory temp-file write.
        """
        try:
            entries = self.read()
            entries.append(entry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = [e.to_dict() for e in entries]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)
        except Exception as exc:
            print(f"metrics write failed: {exc}. Entry: {json.dumps(entry.to_dict())}", file=sys.stderr)

    def read(self) -> list[MetricEntry]:
        if not self.path.exists():
            return []
        try:
            payload: object = json.loads(self.path.read_text(encoding="utf-8"))
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
