import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

TimelineEventType = Literal[
    "attempt_started",
    "task_completed",
    "task_failed",
    "task_needs_human",
    "make_check_passed",
    "make_check_failed",
    "recovery_started",
    "recovery_completed",
    "task_escalated",
    "loop_stopped",
    "run_interrupted",
    "stderr_warning",
    "execution_notice",
    "export_failed",
    "cleanup_failed",
]


@dataclass(frozen=True)
class TimelineEvent:
    ts: str
    event: TimelineEventType
    iteration: int | None = None
    task: str | None = None
    detail: dict[str, object] | None = None

    def to_jsonl(self) -> str:
        payload: dict[str, object] = {"ts": self.ts, "event": self.event}
        if self.iteration is not None:
            payload["iteration"] = self.iteration
        if self.task is not None:
            payload["task"] = self.task
        if self.detail is not None:
            payload["detail"] = self.detail
        return json.dumps(payload, sort_keys=True)


@dataclass
class TimelineStore:
    path: Path

    def record(self, event: TimelineEvent) -> None:
        """Record an event to the timeline, with fallback to stderr on failure.

        This ensures that even if the timeline file cannot be written,
        the event details are still visible to operators.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(event.to_jsonl() + "\n")
        except Exception as exc:
            # Fallback: emit to stderr so the event is not lost
            print(
                f"timeline write failed: {exc}. Event: {event.to_jsonl()}",
                file=sys.stderr,
            )

    def read(self) -> list[TimelineEvent]:
        if not self.path.exists():
            return []
        events: list[TimelineEvent] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(
                TimelineEvent(
                    ts=str(payload.get("ts", "")),
                    event=payload.get("event", ""),
                    iteration=payload.get("iteration"),
                    task=payload.get("task"),
                    detail=payload.get("detail"),
                )
            )
        return events

    @staticmethod
    def now_iso() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
