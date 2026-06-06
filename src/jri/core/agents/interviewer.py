"""Project-intent interviewer agent."""

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
    RunContext,
    TextPartDelta,
    Tool,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
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
from jri.core.tools.note import write_note
from jri.core.tools.spec import replace_spec, write_spec
from jri.core.tools.write import WriteError, parse_patch
from jri.core.triggers import is_trigger_message

SpecPath = Annotated[
    str,
    Field(
        description=(
            "Relative Markdown filename under .jri/specs, without absolute "
            "directories. Use values like product or product.md."
        ),
    ),
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
            events = await self._collect_model_events(
                user_message,
                deps=deps,
                turn_context=turn_context,
            )
        except UnexpectedModelBehavior as exc:
            recovered = await self._append_trigger_fallback_events(
                deps,
                events,
            )
            if not recovered:
                raise
            self.logger.write(
                "model_error_recovered",
                {"message": str(exc), "error_type": type(exc).__name__},
            )
        else:
            await self._append_trigger_fallback_events(deps, events)
        for event in events:
            yield event

    async def _collect_model_events(
        self,
        user_message: str,
        *,
        deps: InterviewerDeps,
        turn_context: str,
    ) -> list[InterviewEvent]:
        buffer = _TurnEventBuffer()
        async with self.agent.run_stream_events(
            user_message,
            message_history=self._messages,
            deps=deps,
            instructions=turn_context,
        ) as stream:
            async for event in stream:
                if self._record_stream_event(event, deps, buffer):
                    break
        return buffer.events

    def _record_stream_event(
        self,
        event: object,
        deps: InterviewerDeps,
        buffer: _TurnEventBuffer,
    ) -> bool:
        if isinstance(event, FunctionToolCallEvent):
            return self._record_tool_call_event(event, buffer)
        if isinstance(event, FunctionToolResultEvent):
            self.logger.write(
                "model_tool_call_finished",
                _model_tool_result_data(event),
            )
        elif isinstance(event, PartDeltaEvent) and isinstance(
            event.delta,
            TextPartDelta,
        ):
            self._record_text_delta_event(event, buffer)
        elif isinstance(event, AgentRunResultEvent):
            self._record_run_result_event(event, deps, buffer)
        return False

    def _record_tool_call_event(
        self,
        event: FunctionToolCallEvent,
        buffer: _TurnEventBuffer,
    ) -> bool:
        self.logger.write(
            "model_tool_call_started",
            _model_tool_call_data(event.part),
        )
        if event.part.tool_name == "ask":
            buffer.events = [
                item
                for item in buffer.events
                if item.kind not in {"text", "text_delta"}
            ]
        buffer.events.append(
            InterviewEvent(kind="tool_call", content=event.part.tool_name)
        )
        if event.part.tool_name != "ask":
            return False
        buffer.events.append(
            InterviewEvent(
                kind="question",
                content=_build_ask_tool_call_question(event.part.args),
            ),
        )
        return True

    def _record_text_delta_event(
        self,
        event: PartDeltaEvent,
        buffer: _TurnEventBuffer,
    ) -> None:
        delta = cast("TextPartDelta", event.delta)
        buffer.saw_text_delta = True
        self.logger.write(
            "model_text_delta",
            {"content": delta.content_delta},
        )
        buffer.events.append(
            InterviewEvent(
                kind="text_delta",
                content=delta.content_delta,
            )
        )

    def _record_run_result_event(
        self,
        event: AgentRunResultEvent[object],
        deps: InterviewerDeps,
        buffer: _TurnEventBuffer,
    ) -> None:
        result = cast("AgentRunResult[str]", event.result)
        self._messages = result.all_messages()[-12:]
        self._should_exit = deps.finalized
        self.logger.write(
            "model_turn_finished",
            _model_turn_finished_data(
                result,
                finalized=deps.finalized,
                retained_message_count=len(self._messages),
            ),
        )
        if not buffer.saw_text_delta:
            buffer.events.append(
                InterviewEvent(kind="text", content=result.output)
            )

    async def _append_trigger_fallback_events(
        self,
        deps: InterviewerDeps,
        events: list[InterviewEvent],
    ) -> bool:
        """Finalize persisted specs after trigger turns."""
        skip_reason = _trigger_fallback_skip_reason(deps, events)
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
            "just_ralph_it",
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
            InterviewEvent(kind="tool_call", content="just_ralph_it"),
            InterviewEvent(kind="text", content=result),
        ])
        self._should_exit = deps.finalized
        return True


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


def build_interviewer_tools() -> list[Tool[InterviewerDeps]]:
    """Build the strict tool set exposed to the interviewer model."""
    return [
        Tool(
            write_spec_tool,
            takes_ctx=True,
            name="spec",
            max_retries=3,
            strict=True,
        ),
        Tool(
            write_note_tool,
            takes_ctx=True,
            name="note",
            max_retries=3,
            strict=True,
        ),
        Tool(
            ask_question_tool,
            takes_ctx=False,
            name="ask",
            strict=True,
        ),
        Tool(
            explore_tool,
            takes_ctx=True,
            name="explore",
            strict=True,
        ),
        Tool(
            finalize_tool,
            takes_ctx=True,
            name="just_ralph_it",
            max_retries=3,
            strict=True,
        ),
    ]


def ask_question_tool(
    level: Literal["high", "low"],
    question: str,
    choices: list[AskChoice] | None = None,
    default: str | None = None,
) -> str:
    """Record the question the interviewer wants answered next."""
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
        for choice in _tool_choices(parsed.get("choices"))
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


def _tool_choices(value: object) -> list[object]:
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


async def explore_tool(
    ctx: RunContext[InterviewerDeps],
    request: str,
) -> str:
    """Gather compact read-only context."""
    return await _run_logged_tool(
        ctx.deps,
        "explore",
        {"request": request},
        lambda: explore_context(
            project_root=ctx.deps.project_root,
            request=request,
            explorer=ctx.deps.explorer,
        ),
    )


async def finalize_tool(
    ctx: RunContext[InterviewerDeps],
    readiness_summary: str,
    spec_content: str,
    spec_path: SpecPath = "product",
    known_blockers: list[str] | None = None,
) -> str:
    """Persist the final spec, commit JRI files, and exit."""
    blocker_log: list[JsonValue] = list(known_blockers or [])

    async def finalize() -> str:
        if not spec_content.strip():
            msg = "just_ralph_it requires non-empty final spec_content."
            raise ModelRetry(msg)
        if not known_blockers:
            try:
                await replace_spec(
                    project_root=ctx.deps.project_root,
                    path=spec_path,
                    content=spec_content,
                )
            except WriteError as exc:
                raise ModelRetry(_spec_path_retry_message(str(exc))) from exc
        try:
            result = await finalize_jri(
                project_root=ctx.deps.project_root,
                latest_user_message=ctx.deps.latest_user_message,
                readiness_summary=readiness_summary,
                known_blockers=known_blockers,
            )
        except JustRalphItError as exc:
            raise ModelRetry(_finalize_retry_message(str(exc))) from exc
        ctx.deps.finalized = result.should_exit
        return result.message

    return await _run_logged_tool(
        ctx.deps,
        "just_ralph_it",
        {"known_blockers": blocker_log, "spec_path": spec_path},
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


def _trigger_fallback_skip_reason(
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
    return None


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
        raise ModelRetry(_patch_retry_message("patch_text is empty"))
    try:
        hunks = parse_patch(patch_text)
    except ValueError as exc:
        raise ModelRetry(_patch_retry_message(str(exc))) from exc
    if not hunks:
        raise ModelRetry(_patch_retry_message("patch_text has no file hunks"))


async def _run_patch_tool(
    action: Callable[[], Awaitable[str]],
    *,
    patch_text: str,
) -> str:
    try:
        _validate_patch_text(patch_text)
        return await action()
    except WriteError as exc:
        raise ModelRetry(_patch_retry_message(str(exc))) from exc


def _patch_retry_message(reason: str) -> str:
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


def _spec_path_retry_message(reason: str) -> str:
    return (
        f"Invalid spec_path: {reason}. Use a relative Markdown filename under "
        ".jri/specs, for example product or product.md. Never pass an "
        "absolute path."
    )


def _finalize_retry_message(reason: str) -> str:
    return (
        f"Invalid just_ralph_it call: {reason} Call just_ralph_it only when "
        "the latest user message is the trigger phrase, and pass known "
        "blockers instead of finalizing when the spec is not ready."
    )


def _model_tool_call_data(part: ToolCallPart) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "tool_name": part.tool_name,
        "tool_call_id": part.tool_call_id,
        "args": _json_value(part.args),
    }
    with contextlib.suppress(Exception):
        payload["args_json"] = part.args_as_json_str()
    return payload


def _model_tool_result_data(
    event: FunctionToolResultEvent,
) -> dict[str, JsonValue]:
    part = event.part
    tool_name = part.tool_name if isinstance(part, ToolReturnPart) else None
    return {
        "tool_name": tool_name,
        "tool_call_id": part.tool_call_id,
        "part_kind": part.part_kind,
        "content": _json_value(part.content),
    }


def _model_turn_finished_data(
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
        data["usage"] = _usage_data(result.usage)
    with contextlib.suppress(Exception):
        data["response"] = _model_response_data(result.response)
    return data


def _model_response_data(response: ModelResponse) -> dict[str, JsonValue]:
    return {
        "model_name": response.model_name,
        "provider_name": response.provider_name,
        "provider_response_id": response.provider_response_id,
        "finish_reason": response.finish_reason,
        "run_id": response.run_id,
        "conversation_id": response.conversation_id,
        "usage": _usage_data(response.usage),
        "part_kinds": [part.part_kind for part in response.parts],
        "text": response.text,
    }


def _usage_data(usage: RunUsage | RequestUsage) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {
        "input_tokens": usage.input_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "output_tokens": usage.output_tokens,
        "input_audio_tokens": usage.input_audio_tokens,
        "cache_audio_read_tokens": usage.cache_audio_read_tokens,
        "output_audio_tokens": usage.output_audio_tokens,
        "total_tokens": usage.total_tokens,
        "details": _json_value(usage.details),
    }
    if isinstance(usage, RunUsage):
        data["requests"] = usage.requests
        data["tool_calls"] = usage.tool_calls
    return data


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in cast("Mapping[object, object]", value).items()
        }
    if isinstance(value, list):
        return [_json_value(item) for item in cast("list[object]", value)]
    if isinstance(value, tuple):
        return [
            _json_value(item) for item in cast("tuple[object, ...]", value)
        ]
    return repr(value)
