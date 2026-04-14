from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Self, cast

Assignee = Literal["Ralph", "Human"]
RalphResult = Literal["completed", "incomplete", "needs_human"]
Result = Literal["completed", "incomplete", "needs_human", "failed", "timeout"]
AttemptResult = Literal[
    "completed",
    "incomplete",
    "needs_human",
    "failed",
    "interrupted",
    "timeout",
]


@dataclass(frozen=True)
class HumanTaskPayload:
    title: str
    body: str
    acceptance_criteria: list[str]
    priority: int | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "title": self.title,
            "body": self.body,
            "acceptance_criteria": self.acceptance_criteria,
        }
        if self.priority is not None:
            payload["priority"] = self.priority
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> Self:
        criteria = payload.get("acceptance_criteria")
        return cls(
            title=_str_or_none(payload.get("title")) or "",
            body=_str_or_none(payload.get("body")) or "",
            acceptance_criteria=(
                [item for item in criteria if isinstance(item, str)]
                if isinstance(criteria, list)
                else []
            ),
            priority=_int_or_none(payload.get("priority")),
        )


@dataclass(frozen=True)
class RalphResultPayload:
    result: RalphResult
    summary: str | None = None
    learnings: list[str] = field(default_factory=list)
    blocker: str | None = None
    human_task: HumanTaskPayload | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"result": self.result}
        if self.summary is not None:
            payload["summary"] = self.summary
        if self.learnings:
            payload["learnings"] = self.learnings
        if self.blocker is not None:
            payload["blocker"] = self.blocker
        if self.human_task is not None:
            payload["human_task"] = self.human_task.to_payload()
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> Self:
        human_task_raw = payload.get("human_task")
        learnings = payload.get("learnings")
        return cls(
            result=cast(RalphResult, payload.get("result")),
            summary=_str_or_none(payload.get("summary")),
            learnings=(
                [item for item in learnings if isinstance(item, str)]
                if isinstance(learnings, list)
                else []
            ),
            blocker=_str_or_none(payload.get("blocker")),
            human_task=(
                HumanTaskPayload.from_payload(cast(dict[str, object], human_task_raw))
                if isinstance(human_task_raw, dict)
                else None
            ),
        )


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
    result: Result = "failed"
    payload: RalphResultPayload | None = None
    warnings: list[str] = field(default_factory=list)


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
    branch: str
    started_at: int
    finished_at: int | None = None
    log_path: str | None = None
    session_id: str | None = None
    result: AttemptResult | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "number": self.number,
            "task_slug": self.task_slug,
            "branch": self.branch,
            "started_at": self.started_at,
        }
        if self.finished_at is not None:
            payload["finished_at"] = self.finished_at
        if self.log_path is not None:
            payload["log_path"] = self.log_path
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        if self.result is not None:
            payload["result"] = self.result
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> Self:
        return cls(
            number=_int_or_default(payload.get("number"), default=0),
            task_slug=_str_or_none(payload.get("task_slug")) or "",
            branch=_str_or_none(payload.get("branch")) or "",
            started_at=_int_or_default(payload.get("started_at"), default=0),
            finished_at=_int_or_none(payload.get("finished_at")),
            log_path=_str_or_none(payload.get("log_path")),
            session_id=_str_or_none(payload.get("session_id")),
            result=_attempt_result_or_none(payload.get("result")),
        )


@dataclass(frozen=True)
class PromotionRecord:
    confirmed_at: int
    task_slugs: list[str]
    target_status: Literal["todo"] = "todo"

    def to_payload(self) -> dict[str, object]:
        return {
            "confirmed_at": self.confirmed_at,
            "task_slugs": self.task_slugs,
            "target_status": self.target_status,
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
        )


@dataclass(frozen=True)
class State:
    started_at: int | None = None
    finished_at: int | None = None
    session: str | None = None
    process: ProcessState | None = None
    branch: str | None = None
    active_attempt: AttemptState | None = None
    attempts: list[AttemptState] = field(default_factory=list)
    promotion: PromotionRecord | None = None
    current_task: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.started_at is not None:
            payload["started_at"] = self.started_at
        if self.finished_at is not None:
            payload["finished_at"] = self.finished_at
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
        if self.current_task is not None:
            payload["current_task"] = self.current_task
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> Self:
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

        return cls(
            started_at=_int_or_none(payload.get("started_at")),
            finished_at=_int_or_none(payload.get("finished_at")),
            session=_str_or_none(payload.get("session")),
            process=process,
            branch=_str_or_none(payload.get("branch")),
            active_attempt=active_attempt,
            attempts=attempts,
            promotion=promotion,
            current_task=_str_or_none(payload.get("current_task")),
        )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _int_or_default(value: object, *, default: int) -> int:
    return value if isinstance(value, int) else default


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _attempt_result_or_none(value: object) -> AttemptResult | None:
    if value in {
        "completed",
        "incomplete",
        "needs_human",
        "failed",
        "interrupted",
        "timeout",
    }:
        return cast(AttemptResult, value)
    return None
