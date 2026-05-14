from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Self, cast

Assignee = Literal["Ralph", "Human"]
TaskStatus = Literal["todo", "doing", "done"]
RalphResult = Literal["completed", "incompleted", "needs_human"]
Result = Literal["completed", "incompleted", "needs_human", "failed", "timeout"]
AttemptResult = Literal[
    "completed",
    "incompleted",
    "needs_human",
    "failed",
    "interrupted",
    "timeout",
]
RuntimeProcessState = Literal["running", "not_running", "stale"]
AttemptLifecycleState = Literal["active", "persisted"]
PayloadLifecycleState = Literal["present", "missing", "invalid"]
LogLifecycleState = Literal["present", "missing", "recovered"]
GraphNodeState = Literal["active", "archived"]

TASK_STATUSES: tuple[TaskStatus, ...] = ("todo", "doing", "done")
LIFECYCLE_TASK_STATUSES: tuple[TaskStatus, ...] = ("todo", "doing", "done")
RALPH_RESULT_VALUES: tuple[RalphResult, ...] = (
    "completed",
    "incompleted",
    "needs_human",
)
RESULT_VALUES: tuple[Result, ...] = (
    "completed",
    "incompleted",
    "needs_human",
    "failed",
    "timeout",
)
ATTEMPT_RESULT_VALUES: tuple[AttemptResult, ...] = (
    "completed",
    "incompleted",
    "needs_human",
    "failed",
    "interrupted",
    "timeout",
)
RUNTIME_PROCESS_STATES: tuple[RuntimeProcessState, ...] = (
    "running",
    "not_running",
    "stale",
)
ATTEMPT_LIFECYCLE_STATES: tuple[AttemptLifecycleState, ...] = (
    "active",
    "persisted",
)
PAYLOAD_LIFECYCLE_STATES: tuple[PayloadLifecycleState, ...] = (
    "present",
    "missing",
    "invalid",
)
LOG_LIFECYCLE_STATES: tuple[LogLifecycleState, ...] = (
    "present",
    "missing",
    "recovered",
)
GRAPH_NODE_STATES: tuple[GraphNodeState, ...] = ("active", "archived")


@dataclass(frozen=True)
class LifecycleInvariant:
    surface: str
    vocabulary: tuple[str, ...]
    invariant: str


JRI_LIFECYCLE_INVARIANTS: tuple[LifecycleInvariant, ...] = (
    LifecycleInvariant(
        surface="task_files",
        vocabulary=TASK_STATUSES,
        invariant=(
            "Ralph selects todo tasks, owns one doing task, and acceptance moves "
            "doing to done"
        ),
    ),
    LifecycleInvariant(
        surface="runtime_process",
        vocabulary=RUNTIME_PROCESS_STATES,
        invariant=(
            "a stale or missing runtime cannot keep a task in doing without recovery"
        ),
    ),
    LifecycleInvariant(
        surface="active_attempt",
        vocabulary=ATTEMPT_LIFECYCLE_STATES,
        invariant=(
            "active_attempt exists only while execution or final bookkeeping is pending"
        ),
    ),
    LifecycleInvariant(
        surface="persisted_attempts",
        vocabulary=ATTEMPT_RESULT_VALUES,
        invariant=(
            "every started Ralph task has an inspectable persisted attempt result"
        ),
    ),
    LifecycleInvariant(
        surface="result_payload",
        vocabulary=PAYLOAD_LIFECYCLE_STATES,
        invariant=(
            "completed, incompleted, and needs_human require a valid Ralph "
            "result payload"
        ),
    ),
    LifecycleInvariant(
        surface="logs",
        vocabulary=LOG_LIFECYCLE_STATES,
        invariant="inspect always uses a saved log or a JRI-generated recovery log",
    ),
    LifecycleInvariant(
        surface="human_blockers",
        vocabulary=("todo", "depends_on", "needs_human"),
        invariant=(
            "needs_human creates a Human todo task and blocks the original "
            "Ralph todo task"
        ),
    ),
)


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
                [item for item in cast(list[object], criteria) if isinstance(item, str)]
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
                [
                    item
                    for item in cast(list[object], learnings)
                    if isinstance(item, str)
                ]
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
class CompilerTaskSpec:
    title: str
    priority: int
    assignee: Assignee
    depends_on: list[str]
    acceptance_criteria: list[str]
    body: str


@dataclass(frozen=True)
class Task:
    path: Path
    slug: str
    metadata: TaskMetadata
    body: str


@dataclass(frozen=True)
class GraphNodeMetadata:
    title: str
    state: GraphNodeState
    archive_reason: str | None = None


@dataclass(frozen=True)
class GraphNode:
    path: Path
    semantic_path: str
    metadata: GraphNodeMetadata
    body: str


@dataclass(frozen=True)
class AgentRunResult:
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
    result_payload: RalphResultPayload | None = None

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
        if self.result_payload is not None:
            payload["result_payload"] = self.result_payload.to_payload()
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
            result_payload=(
                RalphResultPayload.from_payload(
                    cast(dict[str, object], payload["result_payload"])
                )
                if isinstance(payload.get("result_payload"), dict)
                else None
            ),
        )


@dataclass(frozen=True)
class ResetPoint:
    task_slug: str
    host_branch: str
    ralph_branch: str
    before_begin_commit: str
    begin_commit: str
    end_commit: str | None = None
    started_at: int | None = None
    finished_at: int | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "task_slug": self.task_slug,
            "host_branch": self.host_branch,
            "ralph_branch": self.ralph_branch,
            "before_begin_commit": self.before_begin_commit,
            "begin_commit": self.begin_commit,
        }
        if self.end_commit is not None:
            payload["end_commit"] = self.end_commit
        if self.started_at is not None:
            payload["started_at"] = self.started_at
        if self.finished_at is not None:
            payload["finished_at"] = self.finished_at
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> Self:
        return cls(
            task_slug=_str_or_none(payload.get("task_slug")) or "",
            host_branch=_str_or_none(payload.get("host_branch")) or "",
            ralph_branch=_str_or_none(payload.get("ralph_branch")) or "",
            before_begin_commit=(
                _str_or_none(payload.get("before_begin_commit")) or ""
            ),
            begin_commit=_str_or_none(payload.get("begin_commit")) or "",
            end_commit=_str_or_none(payload.get("end_commit")),
            started_at=_int_or_none(payload.get("started_at")),
            finished_at=_int_or_none(payload.get("finished_at")),
        )


RunOutcome = Literal[
    "completed",
    "no_work",
    "task_failure",
    "timeout",
    "needs_human",
]


@dataclass(frozen=True)
class RunSummary:
    completed: int
    outcome: RunOutcome
    task_results: dict[str, Result] = field(default_factory=dict)


@dataclass(frozen=True)
class State:
    started_at: int | None = None
    finished_at: int | None = None
    session: str | None = None
    process: ProcessState | None = None
    branch: str | None = None
    active_attempt: AttemptState | None = None
    attempts: list[AttemptState] = field(default_factory=list)
    current_task: str | None = None
    reset_points: dict[str, dict[str, ResetPoint]] = field(default_factory=dict)

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
        if self.current_task is not None:
            payload["current_task"] = self.current_task
        if self.reset_points:
            payload["reset_points"] = {
                host_branch: {
                    task_slug: reset_point.to_payload()
                    for task_slug, reset_point in task_points.items()
                }
                for host_branch, task_points in self.reset_points.items()
            }
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
                for item in cast(list[object], attempts_raw)
                if isinstance(item, dict)
            ]

        reset_points_raw = payload.get("reset_points")
        reset_points: dict[str, dict[str, ResetPoint]] = {}
        if isinstance(reset_points_raw, dict):
            for host_branch, task_points_raw in cast(
                dict[str, object], reset_points_raw
            ).items():
                if not isinstance(task_points_raw, dict):
                    continue
                task_points: dict[str, ResetPoint] = {}
                for task_slug, reset_point_raw in cast(
                    dict[str, object], task_points_raw
                ).items():
                    if isinstance(reset_point_raw, dict):
                        task_points[task_slug] = ResetPoint.from_payload(
                            cast(dict[str, object], reset_point_raw)
                        )
                if task_points:
                    reset_points[host_branch] = task_points

        return cls(
            started_at=_int_or_none(payload.get("started_at")),
            finished_at=_int_or_none(payload.get("finished_at")),
            session=_str_or_none(payload.get("session")),
            process=process,
            branch=_str_or_none(payload.get("branch")),
            active_attempt=active_attempt,
            attempts=attempts,
            current_task=_str_or_none(payload.get("current_task")),
            reset_points=reset_points,
        )

    def reset_point_for(self, *, host_branch: str, task_slug: str) -> ResetPoint | None:
        return self.reset_points.get(host_branch, {}).get(task_slug)

    def latest_reset_point(
        self, *, host_branch: str | None = None, task_slug: str | None = None
    ) -> ResetPoint | None:
        reset_points = [
            reset_point
            for branch, task_points in self.reset_points.items()
            if host_branch is None or branch == host_branch
            for slug, reset_point in task_points.items()
            if task_slug is None or slug == task_slug
        ]
        if not reset_points:
            return None
        return max(
            reset_points,
            key=lambda reset_point: (
                reset_point.finished_at
                if reset_point.finished_at is not None
                else reset_point.started_at
                if reset_point.started_at is not None
                else -1,
                reset_point.host_branch,
                reset_point.task_slug,
            ),
        )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _int_or_default(value: object, *, default: int) -> int:
    return value if isinstance(value, int) else default


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _attempt_result_or_none(value: object) -> AttemptResult | None:
    if value == "incomplete":
        return "incompleted"
    if value in {
        "completed",
        "incompleted",
        "needs_human",
        "failed",
        "interrupted",
        "timeout",
    }:
        return cast(AttemptResult, value)
    return None
