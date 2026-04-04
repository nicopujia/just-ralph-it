from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Self, cast

Assignee = Literal["Ralph", "Human"]
Outcome = Literal["completed", "failed", "needs human"]
AttemptOutcome = Literal["completed", "failed", "needs human", "interrupted"]


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
    outcome: Outcome = "failed"


@dataclass(frozen=True)
class ProcessState:
    loop_pid: int | None = None
    child_pid: int | None = None
    log_path: str | None = None
    detached: bool = False


@dataclass(frozen=True)
class AttemptState:
    number: int
    task_slug: str
    iteration_number: int
    branch: str
    started_at: int
    finished_at: int | None = None
    log_path: str | None = None
    session_id: str | None = None
    outcome: AttemptOutcome | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "number": self.number,
            "task_slug": self.task_slug,
            "iteration_number": self.iteration_number,
            "branch": self.branch,
            "started_at": self.started_at,
        }
        if self.finished_at is not None:
            payload["finished_at"] = self.finished_at
        if self.log_path is not None:
            payload["log_path"] = self.log_path
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        if self.outcome is not None:
            payload["outcome"] = self.outcome
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> Self:
        return cls(
            number=_int_or_default(payload.get("number"), default=0),
            task_slug=_str_or_none(payload.get("task_slug")) or "",
            iteration_number=_int_or_default(
                payload.get("iteration_number"),
                default=0,
            ),
            branch=_str_or_none(payload.get("branch")) or "",
            started_at=_int_or_default(payload.get("started_at"), default=0),
            finished_at=_int_or_none(payload.get("finished_at")),
            log_path=_str_or_none(payload.get("log_path")),
            session_id=_str_or_none(payload.get("session_id")),
            outcome=_attempt_outcome_or_none(payload.get("outcome")),
        )


@dataclass(frozen=True)
class PromotionRecord:
    confirmed_at: int
    task_slugs: list[str]
    user_confirmation: str
    target_status: Literal["todo"] = "todo"

    def to_payload(self) -> dict[str, object]:
        return {
            "confirmed_at": self.confirmed_at,
            "task_slugs": self.task_slugs,
            "target_status": self.target_status,
            "user_confirmation": self.user_confirmation,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> Self:
        task_slugs_raw = payload.get("task_slugs")
        task_slugs = (
            [item for item in task_slugs_raw if isinstance(item, str)]
            if isinstance(task_slugs_raw, list)
            else []
        )
        return cls(
            confirmed_at=_int_or_default(payload.get("confirmed_at"), default=0),
            task_slugs=task_slugs,
            target_status="todo",
            user_confirmation=_str_or_none(payload.get("user_confirmation")) or "",
        )


@dataclass(frozen=True)
class State:
    iteration_number: int = 0
    started_at: int | None = None
    finished_at: int | None = None
    session: str | None = None
    process: ProcessState | None = None
    branch: str | None = None
    active_attempt: AttemptState | None = None
    attempts: list[AttemptState] = field(default_factory=list)
    promotion: PromotionRecord | None = None

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
        if self.active_attempt is not None:
            payload["active_attempt"] = self.active_attempt.to_payload()
        if self.attempts:
            payload["attempts"] = [attempt.to_payload() for attempt in self.attempts]
        if self.promotion is not None:
            payload["promotion"] = self.promotion.to_payload()
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

        active_attempt_raw = payload.get("active_attempt")
        active_attempt = None
        if isinstance(active_attempt_raw, dict):
            active_attempt = AttemptState.from_payload(
                cast(dict[str, object], active_attempt_raw)
            )

        attempts_raw = payload.get("attempts")
        attempts: list[AttemptState] = []
        if isinstance(attempts_raw, list):
            attempts = [
                AttemptState.from_payload(cast(dict[str, object], item))
                for item in attempts_raw
                if isinstance(item, dict)
            ]

        promotion_raw = payload.get("promotion")
        promotion = None
        if isinstance(promotion_raw, dict):
            promotion = PromotionRecord.from_payload(
                cast(dict[str, object], promotion_raw)
            )

        number = iteration_payload.get("number")
        return cls(
            iteration_number=number if isinstance(number, int) else 0,
            started_at=_int_or_none(iteration_payload.get("started_at")),
            finished_at=_int_or_none(iteration_payload.get("finished_at")),
            session=_str_or_none(payload.get("session")),
            process=process,
            branch=_str_or_none(payload.get("branch")),
            active_attempt=active_attempt,
            attempts=attempts,
            promotion=promotion,
        )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _int_or_default(value: object, *, default: int) -> int:
    return value if isinstance(value, int) else default


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _attempt_outcome_or_none(value: object) -> AttemptOutcome | None:
    if value in {"completed", "failed", "needs human", "interrupted"}:
        return cast(AttemptOutcome, value)
    return None
