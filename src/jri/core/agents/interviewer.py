"""Project-intent interviewer agent."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Annotated, Literal, cast

from pydantic import Field
from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRetry,
    PartDeltaEvent,
    PartStartEvent,
    RunContext,
    TextPartDelta,
    Tool,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.run import AgentRunResult
from pydantic_ai.usage import RequestUsage, RunUsage

from jri.core.agents.explorer import Explorer as ContextExplorerAgent
from jri.core.agents.models import AgentModelConfig
from jri.core.agents.prompts import (
    BASE_INTERVIEWER_PROMPT,
    build_interviewer_context,
)
from jri.core.interview import InterviewEvent, InterviewQuestion
from jri.core.logging import JsonlLogger, JsonValue
from jri.core.tools.ask import AskChoice, build_question
from jri.core.tools.explore import ContextExplorer, explore_context
from jri.core.tools.just_ralph_it import JustRalphItError, finalize_jri
from jri.core.tools.note import replace_note, write_note
from jri.core.tools.spec import replace_spec, write_spec
from jri.core.tools.write import WriteError, parse_patch
from jri.core.triggers import is_trigger_message

_ASK_TOOL_NAME = "ask_question"

SpecPath = Annotated[
    str,
    Field(
        description=(
            "Relative Markdown filename under .jri/specs, without absolute "
            "directories. Use values like product or product.md."
        ),
    ),
]
SpecName = Annotated[
    str,
    Field(description="Name of the spec to update, such as product."),
]
SpecContent = Annotated[
    str,
    Field(description="Complete confirmed Markdown content for this spec."),
]
NotesContent = Annotated[
    str,
    Field(
        description=(
            "Markdown notes for unresolved branches, pending questions, "
            "assumptions, or decisions."
        ),
    ),
]
ContextRequest = Annotated[
    str,
    Field(
        description=(
            "Specific context to gather before deciding what to ask or record."
        ),
    ),
]
QuestionLevel = Annotated[
    Literal["high", "low"],
    Field(
        description=(
            "Question scope: high for broad product direction, low for "
            "specific behavior choices."
        ),
    ),
]
QuestionText = Annotated[
    str,
    Field(description="Question to ask the user next."),
]
DefaultChoice = Annotated[
    str | None,
    Field(description="Optional default choice label to preselect."),
]
ReadinessSummary = Annotated[
    str,
    Field(description="Why the confirmed specs are ready to finalize."),
]
FinalSpecContent = Annotated[
    str,
    Field(description="Complete final Markdown spec content to save."),
]
KnownBlockers = Annotated[
    list[str] | None,
    Field(description="Missing decisions that prevent finalization."),
]
PatchText = Annotated[
    str,
    Field(
        description=(
            "Complete patch envelope. Use *** Begin Patch and *** End Patch. "
            "For Add File, every body line must start with +, including blank "
            "Markdown lines written as +."
        ),
    ),
]


@dataclass
class InterviewerDeps:
    """Dependencies available to interviewer tools."""

    project_root: Path
    latest_user_message: str
    logger: JsonlLogger
    explorer: ContextExplorer
    finalized: bool = False


@dataclass
class _TurnEventBuffer:
    """Mutable event buffer for one streamed model turn."""

    events: list[InterviewEvent] = field(default_factory=list)
    saw_text_delta: bool = False


@dataclass(frozen=True)
class _ModelStreamError:
    """Exception raised while consuming provider stream events."""

    error: Exception


class _ModelStreamFinished:
    """Sentinel for provider stream completion."""


_MODEL_STREAM_FINISHED = _ModelStreamFinished()


class Interviewer:
    """Live interviewer that maintains JRI interview state."""

    def __init__(
        self,
        *,
        project_root: Path,
        logger: JsonlLogger,
        model_config: AgentModelConfig,
    ) -> None:
        self.project_root: Path = project_root
        self.logger: JsonlLogger = logger
        self._model_config: AgentModelConfig = model_config
        self._messages: list[ModelMessage] = []
        self._should_exit: bool = False
        self.refresh_model_clients()

    def refresh_model_clients(self) -> None:
        """Rebuild model clients while retaining interview state."""
        self.explorer: ContextExplorerAgent = ContextExplorerAgent(
            model=self._model_config.explorer
        )
        self.agent: Agent[InterviewerDeps, str] = Agent(
            self._model_config.interviewer,
            deps_type=InterviewerDeps,
            instructions=BASE_INTERVIEWER_PROMPT,
            tools=build_interviewer_tools(),
            end_strategy="exhaustive",
        )

    @property
    def should_exit(self) -> bool:
        """Return whether finalization completed."""
        return self._should_exit

    async def respond(
        self,
        user_message: str,
    ) -> AsyncIterator[InterviewEvent]:
        """Run one live interviewer turn."""
        deps = InterviewerDeps(
            project_root=self.project_root,
            latest_user_message=user_message,
            logger=self.logger,
            explorer=self.explorer,
        )
        turn_context = build_interviewer_context(self.project_root)
        self.logger.write(
            "model_turn_started",
            {
                "user_message": user_message,
                "message_history_count": len(self._messages),
                "context": turn_context,
            },
        )
        events: list[InterviewEvent] = []
        try:
            async for event in self._iter_model_events(
                user_message,
                deps=deps,
                turn_context=turn_context,
            ):
                events.append(event)
                yield event
        except UnexpectedModelBehavior:
            await self._append_trigger_fallback_events(deps, events)
            raise
        else:
            yielded_event_count = len(events)
            await self._append_trigger_fallback_events(deps, events)
            for event in events[yielded_event_count:]:
                yield event

    async def _iter_model_events(
        self,
        user_message: str,
        *,
        deps: InterviewerDeps,
        turn_context: str,
    ) -> AsyncIterator[InterviewEvent]:
        queue: asyncio.Queue[object] = asyncio.Queue()
        producer = asyncio.create_task(
            self._produce_model_events(
                user_message,
                deps=deps,
                turn_context=turn_context,
                queue=queue,
            )
        )
        try:
            while True:
                item = await queue.get()
                if item is _MODEL_STREAM_FINISHED:
                    break
                if isinstance(item, _ModelStreamError):
                    raise item.error
                yield cast("InterviewEvent", item)
        finally:
            if not producer.done():
                producer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await producer

    async def _produce_model_events(
        self,
        user_message: str,
        *,
        deps: InterviewerDeps,
        turn_context: str,
        queue: asyncio.Queue[object],
    ) -> None:
        try:
            pending_text_events = await self._consume_model_stream(
                user_message,
                deps=deps,
                turn_context=turn_context,
                queue=queue,
            )
        except Exception as exc:  # noqa: BLE001 - cross task error handoff
            queue.put_nowait(_ModelStreamError(exc))
        else:
            _queue_events(queue, pending_text_events)
            queue.put_nowait(_MODEL_STREAM_FINISHED)

    async def _consume_model_stream(
        self,
        user_message: str,
        *,
        deps: InterviewerDeps,
        turn_context: str,
        queue: asyncio.Queue[object],
    ) -> list[InterviewEvent]:
        buffer = _TurnEventBuffer()
        pending_text_events: list[InterviewEvent] = []
        async with self.agent.run_stream_events(
            user_message,
            message_history=self._messages,
            deps=deps,
            instructions=turn_context,
        ) as stream:
            async for event in stream:
                visible_events, should_stop = self._record_stream_event(
                    event,
                    deps,
                    buffer,
                )
                _queue_recorded_events(
                    event,
                    visible_events=visible_events,
                    queue=queue,
                    pending_text_events=pending_text_events,
                )
                if should_stop:
                    break
        return pending_text_events

    def _record_stream_event(
        self,
        event: object,
        deps: InterviewerDeps,
        buffer: _TurnEventBuffer,
    ) -> tuple[list[InterviewEvent], bool]:
        if isinstance(event, FunctionToolCallEvent):
            return self._record_tool_call_event(event, buffer)
        if isinstance(event, FunctionToolResultEvent):
            self.logger.write(
                "model_tool_call_finished",
                _serialize_model_tool_result(event),
            )
            return [], False
        if isinstance(event, PartDeltaEvent) and isinstance(
            event.delta,
            TextPartDelta,
        ):
            return [self._record_text_delta_event(event, buffer)], False
        if isinstance(event, PartStartEvent) and isinstance(
            event.part,
            TextPart,
        ):
            return [self._record_text_start_event(event, buffer)], False
        if isinstance(event, AgentRunResultEvent):
            return self._record_run_result_event(event, deps, buffer), False
        return [], False

    def _record_tool_call_event(
        self,
        event: FunctionToolCallEvent,
        buffer: _TurnEventBuffer,
    ) -> tuple[list[InterviewEvent], bool]:
        self.logger.write(
            "model_tool_call_started",
            _serialize_model_tool_call(event.part),
        )
        if event.part.tool_name == _ASK_TOOL_NAME:
            buffer.events = [
                item
                for item in buffer.events
                if item.kind not in {"text", "text_delta"}
            ]
        visible_events = [
            InterviewEvent(kind="tool_call", content=event.part.tool_name)
        ]
        buffer.events.extend(visible_events)
        if event.part.tool_name != _ASK_TOOL_NAME:
            return visible_events, False
        question = _build_ask_tool_call_question(event.part.args)
        visible_events.append(
            InterviewEvent(
                kind="question",
                content=question,
            )
        )
        buffer.events.append(
            InterviewEvent(
                kind="question",
                content=question,
            ),
        )
        self._append_question_to_history(question)
        return visible_events, True

    def _record_text_delta_event(
        self,
        event: PartDeltaEvent,
        buffer: _TurnEventBuffer,
    ) -> InterviewEvent:
        delta = cast("TextPartDelta", event.delta)
        buffer.saw_text_delta = True
        self.logger.write(
            "model_text_delta",
            {"content": delta.content_delta},
        )
        visible_event = InterviewEvent(
            kind="text_delta",
            content=delta.content_delta,
        )
        buffer.events.append(visible_event)
        return visible_event

    def _record_text_start_event(
        self,
        event: PartStartEvent,
        buffer: _TurnEventBuffer,
    ) -> InterviewEvent:
        part = cast("TextPart", event.part)
        buffer.saw_text_delta = True
        self.logger.write(
            "model_text_delta",
            {"content": part.content},
        )
        visible_event = InterviewEvent(
            kind="text_delta",
            content=part.content,
        )
        buffer.events.append(visible_event)
        return visible_event

    def _record_run_result_event(
        self,
        event: AgentRunResultEvent[object],
        deps: InterviewerDeps,
        buffer: _TurnEventBuffer,
    ) -> list[InterviewEvent]:
        result = cast("AgentRunResult[str]", event.result)
        self._messages = result.all_messages()[-12:]
        self._should_exit = deps.finalized
        self.logger.write(
            "model_turn_finished",
            _serialize_model_turn_finished(
                result,
                finalized=deps.finalized,
                retained_message_count=len(self._messages),
            ),
        )
        if not buffer.saw_text_delta:
            visible_event = InterviewEvent(kind="text", content=result.output)
            buffer.events.append(visible_event)
            return [visible_event]
        return []

    def _append_question_to_history(self, question: InterviewQuestion) -> None:
        self._messages = [
            *self._messages,
            ModelResponse(
                parts=[TextPart(_format_question_history(question))]
            ),
        ][-12:]

    async def _append_trigger_fallback_events(
        self,
        deps: InterviewerDeps,
        events: list[InterviewEvent],
    ) -> bool:
        """Finalize persisted specs after trigger turns."""
        skip_reason = _find_trigger_fallback_skip_reason(deps, events)
        if skip_reason is not None:
            if is_trigger_message(deps.latest_user_message):
                self.logger.write(
                    "trigger_fallback_skipped",
                    {"reason": skip_reason},
                )
            return False

        readiness_summary = (
            "Explicit trigger received after persisted specs were captured."
        )
        result = await _run_logged_tool(
            deps,
            "finalize_specs",
            {
                "source": "trigger_fallback",
                "readiness_summary": readiness_summary,
                "known_blockers": [],
            },
            lambda: _finalize_trigger_fallback(
                deps,
                readiness_summary=readiness_summary,
            ),
        )
        events.extend([
            InterviewEvent(kind="tool_call", content="finalize_specs"),
            InterviewEvent(kind="text", content=result),
        ])
        self._should_exit = deps.finalized
        return True


def _should_drop_pending_text_events(event: object) -> bool:
    return (
        isinstance(event, FunctionToolCallEvent)
        and event.part.tool_name == _ASK_TOOL_NAME
    )


def _is_model_text_event(event: object) -> bool:
    return (
        isinstance(event, PartDeltaEvent)
        and isinstance(event.delta, TextPartDelta)
    ) or (
        isinstance(event, PartStartEvent) and isinstance(event.part, TextPart)
    )


def _queue_recorded_events(
    event: object,
    *,
    visible_events: list[InterviewEvent],
    queue: asyncio.Queue[object],
    pending_text_events: list[InterviewEvent],
) -> None:
    if _should_drop_pending_text_events(event):
        pending_text_events.clear()
    if _is_model_text_event(event):
        pending_text_events.extend(visible_events)
        return
    if isinstance(event, AgentRunResultEvent):
        _queue_events(queue, pending_text_events)
        pending_text_events.clear()
    _queue_events(queue, visible_events)


def _queue_events(
    queue: asyncio.Queue[object],
    events: list[InterviewEvent],
) -> None:
    for event in events:
        queue.put_nowait(event)


async def write_spec_tool(
    ctx: RunContext[InterviewerDeps],
    path: SpecPath,
    patch_text: PatchText,
) -> str:
    """Patch one curated project spec file with a structured patch."""
    return await _run_logged_tool(
        ctx.deps,
        "spec",
        {"path": path, "patch_text": patch_text},
        lambda: _run_patch_tool(
            lambda: write_spec(
                project_root=ctx.deps.project_root,
                path=path,
                patch_text=patch_text,
            ),
            patch_text=patch_text,
        ),
    )


async def write_note_tool(
    ctx: RunContext[InterviewerDeps],
    patch_text: PatchText,
) -> str:
    """Patch the interviewer scratchpad with a structured patch."""
    return await _run_logged_tool(
        ctx.deps,
        "note",
        {"patch_text": patch_text},
        lambda: _run_patch_tool(
            lambda: write_note(
                project_root=ctx.deps.project_root,
                patch_text=patch_text,
            ),
            patch_text=patch_text,
        ),
    )


async def update_specs_tool(
    ctx: RunContext[InterviewerDeps],
    spec_name: SpecName,
    content: SpecContent,
) -> str:
    """Create or replace a confirmed project spec by name."""
    return await _run_logged_tool(
        ctx.deps,
        "update_specs",
        {"spec_name": spec_name, "content": content},
        lambda: replace_spec(
            project_root=ctx.deps.project_root,
            path=spec_name,
            content=content,
        ),
    )


async def record_notes_tool(
    ctx: RunContext[InterviewerDeps],
    notes: NotesContent,
) -> str:
    """Record interview notes for unresolved context."""
    return await _run_logged_tool(
        ctx.deps,
        "record_notes",
        {"notes": notes},
        lambda: replace_note(
            project_root=ctx.deps.project_root,
            content=notes,
        ),
    )


def build_interviewer_tools() -> list[Tool[InterviewerDeps]]:
    """Build the strict tool set exposed to the interviewer model."""
    return [
        Tool(
            ask_question_tool,
            takes_ctx=False,
            name="ask_question",
            strict=True,
        ),
        Tool(
            record_notes_tool,
            takes_ctx=True,
            name="record_notes",
            max_retries=3,
            strict=True,
        ),
        Tool(
            update_specs_tool,
            takes_ctx=True,
            name="update_specs",
            max_retries=3,
            strict=True,
        ),
        Tool(
            explore_context_tool,
            takes_ctx=True,
            name="explore_context",
            strict=True,
        ),
        Tool(
            finalize_specs_tool,
            takes_ctx=True,
            name="finalize_specs",
            max_retries=3,
            strict=True,
        ),
    ]


def ask_question_tool(
    level: QuestionLevel,
    question: QuestionText,
    choices: list[AskChoice] | None = None,
    default: DefaultChoice = None,
) -> str:
    """Ask the next high-leverage question for the user to answer."""
    _ = build_question(
        level=level, question=question, choices=choices, default=default
    )
    return "Question recorded for the next user turn."


def _build_ask_tool_call_question(
    args: str | dict[str, object] | None,
) -> InterviewQuestion:
    parsed = _parse_tool_args(args)
    choices = [
        AskChoice.model_validate(choice)
        for choice in _coerce_tool_choices(parsed.get("choices"))
    ]
    level = parsed.get("level", "high")
    if level not in {"high", "low"}:
        level = "high"
    return build_question(
        level=cast("Literal['high', 'low']", level),
        question=str(parsed.get("question", "What should we clarify next?")),
        choices=choices,
        default=(
            str(parsed["default"])
            if parsed.get("default") is not None
            else None
        ),
    )


def _format_question_history(question: InterviewQuestion) -> str:
    parts = [f"Question ({question.level}): {question.question}"]
    if question.choices:
        choices = "; ".join(
            (
                f"{choice.label} - {choice.description}"
                if choice.description
                else choice.label
            )
            for choice in question.choices
        )
        parts.append(f"Choices: {choices}")
    if question.default:
        parts.append(f"Default: {question.default}")
    return "\n".join(parts)


def _coerce_tool_choices(value: object) -> list[object]:
    if isinstance(value, list):
        return cast("list[object]", value)
    return []


def _parse_tool_args(
    args: str | dict[str, object] | None,
) -> dict[str, object]:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = cast("object", json.loads(args))
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return cast("dict[str, object]", parsed)
    return {}


async def explore_context_tool(
    ctx: RunContext[InterviewerDeps],
    request: ContextRequest,
) -> str:
    """Explore read-only context before deciding."""
    return await _run_logged_tool(
        ctx.deps,
        "explore_context",
        {"request": request},
        lambda: explore_context(
            project_root=ctx.deps.project_root,
            request=request,
            explorer=ctx.deps.explorer,
        ),
    )


async def finalize_specs_tool(
    ctx: RunContext[InterviewerDeps],
    readiness_summary: ReadinessSummary,
    spec_content: FinalSpecContent,
    spec_name: SpecName = "product",
    known_blockers: KnownBlockers = None,
) -> str:
    """Finalize saved specs and finish the interview."""
    blocker_log: list[JsonValue] = list(known_blockers or [])

    async def finalize() -> str:
        if not spec_content.strip():
            msg = "finalize_specs requires non-empty final spec_content."
            raise ModelRetry(msg)
        if not is_trigger_message(ctx.deps.latest_user_message):
            raise ModelRetry(
                _format_finalize_retry_message(
                    "finalize_specs requires a trigger phrase."
                )
            )
        if known_blockers:
            raise ModelRetry(
                _format_finalize_retry_message("\n".join(known_blockers))
            )
        try:
            await replace_spec(
                project_root=ctx.deps.project_root,
                path=spec_name,
                content=spec_content,
            )
        except WriteError as exc:
            raise ModelRetry(_format_spec_name_retry_message()) from exc
        try:
            result = await finalize_jri(
                project_root=ctx.deps.project_root,
                latest_user_message=ctx.deps.latest_user_message,
                readiness_summary=readiness_summary,
                known_blockers=known_blockers,
            )
        except JustRalphItError as exc:
            raise ModelRetry(_format_finalize_retry_message(str(exc))) from exc
        ctx.deps.finalized = result.should_exit
        return result.message

    return await _run_logged_tool(
        ctx.deps,
        "finalize_specs",
        {"known_blockers": blocker_log, "spec_name": spec_name},
        finalize,
    )


async def _run_logged_tool(
    deps: InterviewerDeps,
    tool_name: str,
    arguments: Mapping[str, JsonValue],
    action: Callable[[], Awaitable[str]],
) -> str:
    started_at = perf_counter()
    deps.logger.write(
        "tool_call_started",
        {"tool_name": tool_name, "arguments": dict(arguments)},
    )
    try:
        result = await action()
    except Exception as exc:
        deps.logger.write(
            "tool_call_failed",
            {
                "tool_name": tool_name,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "duration_ms": round((perf_counter() - started_at) * 1000, 3),
            },
        )
        raise
    deps.logger.write(
        "tool_call_finished",
        {
            "tool_name": tool_name,
            "result": result,
            "duration_ms": round((perf_counter() - started_at) * 1000, 3),
        },
    )
    return result


def _find_trigger_fallback_skip_reason(
    deps: InterviewerDeps,
    events: list[InterviewEvent],
) -> str | None:
    if deps.finalized:
        return "already_finalized"
    if not is_trigger_message(deps.latest_user_message):
        return "not_trigger"
    if any(event.kind == "question" for event in events):
        return "model_asked_question"
    if not _has_persisted_spec(deps.project_root):
        return "no_persisted_spec"
    if not _has_trigger_fallback_readiness(events):
        return "no_readiness_signal"
    return None


def _has_trigger_fallback_readiness(events: list[InterviewEvent]) -> bool:
    text = "".join(
        event.content
        for event in events
        if event.kind in {"text", "text_delta"}
        and isinstance(event.content, str)
    )
    normalized = text.strip().lower().rstrip(".!")
    return normalized in {
        "ready for ralph handoff",
        "ready to hand off",
    }


async def _finalize_trigger_fallback(
    deps: InterviewerDeps,
    *,
    readiness_summary: str,
) -> str:
    result = await finalize_jri(
        project_root=deps.project_root,
        latest_user_message=deps.latest_user_message,
        readiness_summary=readiness_summary,
        known_blockers=[],
    )
    deps.finalized = result.should_exit
    return result.message


def _has_persisted_spec(project_root: Path) -> bool:
    specs_dir = project_root / ".jri" / "specs"
    return specs_dir.exists() and any(specs_dir.glob("**/*.md"))


def _validate_patch_text(patch_text: str) -> None:
    if not patch_text.strip():
        raise ModelRetry(_format_patch_retry_message("patch_text is empty"))
    try:
        hunks = parse_patch(patch_text)
    except ValueError as exc:
        raise ModelRetry(_format_patch_retry_message(str(exc))) from exc
    if not hunks:
        raise ModelRetry(
            _format_patch_retry_message("patch_text has no file hunks")
        )


async def _run_patch_tool(
    action: Callable[[], Awaitable[str]],
    *,
    patch_text: str,
) -> str:
    try:
        _validate_patch_text(patch_text)
        return await action()
    except WriteError as exc:
        raise ModelRetry(_format_patch_retry_message(str(exc))) from exc


def _format_patch_retry_message(reason: str) -> str:
    return (
        f"Invalid patch_text: {reason}. Send patch_text as a complete patch "
        "envelope. For Add File, every content line must start with +, and "
        "blank Markdown lines must be written as +. Also pass spec paths as "
        "relative names like product, never absolute paths. Example:\n"
        "*** Begin Patch\n"
        "*** Add File: product.md\n"
        "+# Product\n"
        "+\n"
        "+First paragraph.\n"
        "*** End Patch"
    )


def _format_spec_name_retry_message() -> str:
    return "Invalid spec_name. Use a spec name such as product or product.md."


def _format_finalize_retry_message(reason: str) -> str:
    return (
        f"Invalid finalize_specs call: {reason} Call finalize_specs only when "
        "the latest user message is the trigger phrase, and pass known "
        "blockers instead of finalizing when the spec is not ready."
    )


def _serialize_model_tool_call(part: ToolCallPart) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "tool_name": part.tool_name,
        "tool_call_id": part.tool_call_id,
        "args": _coerce_json_value(part.args),
    }
    with contextlib.suppress(Exception):
        payload["args_json"] = part.args_as_json_str()
    return payload


def _serialize_model_tool_result(
    event: FunctionToolResultEvent,
) -> dict[str, JsonValue]:
    part = event.part
    tool_name = part.tool_name if isinstance(part, ToolReturnPart) else None
    return {
        "tool_name": tool_name,
        "tool_call_id": part.tool_call_id,
        "part_kind": part.part_kind,
        "content": _coerce_json_value(part.content),
    }


def _serialize_model_turn_finished(
    result: AgentRunResult[str],
    *,
    finalized: bool,
    retained_message_count: int,
) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {
        "output": result.output,
        "finalized": finalized,
        "retained_message_count": retained_message_count,
    }
    with contextlib.suppress(Exception):
        data["run_id"] = result.run_id
    with contextlib.suppress(Exception):
        data["conversation_id"] = result.conversation_id
    with contextlib.suppress(Exception):
        data["usage"] = _serialize_usage(result.usage)
    with contextlib.suppress(Exception):
        data["response"] = _serialize_model_response(result.response)
    return data


def _serialize_model_response(
    response: ModelResponse,
) -> dict[str, JsonValue]:
    return {
        "model_name": response.model_name,
        "provider_name": response.provider_name,
        "provider_response_id": response.provider_response_id,
        "finish_reason": response.finish_reason,
        "run_id": response.run_id,
        "conversation_id": response.conversation_id,
        "usage": _serialize_usage(response.usage),
        "part_kinds": [part.part_kind for part in response.parts],
        "text": response.text,
    }


def _serialize_usage(usage: RunUsage | RequestUsage) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {
        "input_tokens": usage.input_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "output_tokens": usage.output_tokens,
        "input_audio_tokens": usage.input_audio_tokens,
        "cache_audio_read_tokens": usage.cache_audio_read_tokens,
        "output_audio_tokens": usage.output_audio_tokens,
        "total_tokens": usage.total_tokens,
        "details": _coerce_json_value(usage.details),
    }
    if isinstance(usage, RunUsage):
        data["requests"] = usage.requests
        data["tool_calls"] = usage.tool_calls
    return data


def _coerce_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _coerce_json_value(item)
            for key, item in cast("Mapping[object, object]", value).items()
        }
    if isinstance(value, list):
        return [
            _coerce_json_value(item) for item in cast("list[object]", value)
        ]
    if isinstance(value, tuple):
        return [
            _coerce_json_value(item)
            for item in cast("tuple[object, ...]", value)
        ]
    return repr(value)
