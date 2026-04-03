from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Self, cast

Assignee = Literal["Ralph", "Human"]
Outcome = Literal["completed", "blocked", "failed", "unknown"]


@dataclass(frozen=True)
class TaskMetadata:
    title: str
    priority: int
    assignee: Assignee
    depends_on: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Task:
    path: Path
    slug: str
    metadata: TaskMetadata
    body: str


@dataclass(frozen=True)
class OpenCodeRunResult:
    returncode: int
    session_id: str | None = None
    outcome: Outcome = "unknown"


@dataclass(frozen=True)
class ProcessState:
    loop_pid: int | None = None
    child_pid: int | None = None
    log_path: str | None = None
    detached: bool = False


@dataclass(frozen=True)
class State:
    iteration_number: int = 0
    started_at: int | None = None
    finished_at: int | None = None
    session: str | None = None
    process: ProcessState | None = None
    branch: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"iteration": {"number": self.iteration_number}}
        iteration = payload["iteration"]
        assert isinstance(iteration, dict)
        if self.started_at is not None:
            iteration["started_at"] = self.started_at
        if self.finished_at is not None:
            iteration["finished_at"] = self.finished_at
        if self.session is not None:
            payload["session"] = self.session
        if self.branch is not None:
            payload["branch"] = self.branch
        if self.process is not None:
            payload["process"] = {
                "loop_pid": self.process.loop_pid,
                "child_pid": self.process.child_pid,
                "log_path": self.process.log_path,
                "detached": self.process.detached,
            }
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> Self:
        iteration_raw = payload.get("iteration", {})
        iteration_payload = cast(
            dict[str, object], iteration_raw if isinstance(iteration_raw, dict) else {}
        )

        process_raw = payload.get("process")
        process = None
        if isinstance(process_raw, dict):
            process_payload = cast(dict[str, object], process_raw)
            process = ProcessState(
                loop_pid=_int_or_none(process_payload.get("loop_pid")),
                child_pid=_int_or_none(process_payload.get("child_pid")),
                log_path=_str_or_none(process_payload.get("log_path")),
                detached=bool(process_payload.get("detached", False)),
            )

        number = iteration_payload.get("number")
        return cls(
            iteration_number=number if isinstance(number, int) else 0,
            started_at=_int_or_none(iteration_payload.get("started_at")),
            finished_at=_int_or_none(iteration_payload.get("finished_at")),
            session=_str_or_none(payload.get("session")),
            process=process,
            branch=_str_or_none(payload.get("branch")),
        )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
