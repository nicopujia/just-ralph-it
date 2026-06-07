"""Project-intent interviewer agent."""

import asyncio
import contextlib
import importlib
import json
import re
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    TextPartDelta,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.run import AgentRunResult
from pydantic_ai.usage import RequestUsage, RunUsage

from jri.core.agents.explorer import Explorer as ContextExplorerAgent
from jri.core.agents.models import AgentModelConfig
from jri.core.agents.prompts import (
    BASE_INTERVIEWER_PROMPT,
    build_interviewer_context,
)
from jri.core.config import (
    AgentRuntimeConfig,
    ConfigError,
    load_agent_runtime_config,
    validate_agent_runtime_credentials,
)
from jri.core.interview import (
    InterviewEvent,
    InterviewQuestion,
    InterviewSession,
)
from jri.core.logging import JsonlLogger, JsonValue
from jri.core.readiness import (
    check_mvp_readiness,
    format_missing_mvp_readiness,
)
from jri.core.tools.ask import AskChoice, build_question
from jri.core.tools.explore import ContextExplorer
from jri.core.tools.interviewer import (
    ask_question_tool,
    build_interviewer_tools,
    explore_context_tool,
    finalize_specs_tool,
    record_notes_tool,
    run_logged_tool,
    update_scratchpad_tool,
    update_specs_tool,
    write_note_tool,
    write_spec_tool,
)
from jri.core.tools.just_ralph_it import finalize_jri
from jri.core.triggers import is_trigger_message

_ASK_TOOL_NAME = "ask_question"
INTERVIEWER_FACTORY_ENV = "JRI_INTERVIEWER_FACTORY"
type InterviewerFactory = Callable[
    [Path, JsonlLogger],
    InterviewSession,
]

__all__ = [
    "INTERVIEWER_FACTORY_ENV",
    "Interviewer",
    "InterviewerDeps",
    "InterviewerFactory",
    "ask_question_tool",
    "build_interviewer_tools",
    "create_interviewer",
    "explore_context_tool",
    "finalize_specs_tool",
    "record_notes_tool",
    "update_scratchpad_tool",
    "update_specs_tool",
    "validate_interviewer_configuration",
    "write_note_tool",
    "write_spec_tool",
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


def create_interviewer(
    *,
    project_root: Path,
    logger: JsonlLogger,
    env: Mapping[str, str],
    runtime_config: AgentRuntimeConfig | None = None,
) -> InterviewSession:
    """Create the configured interviewer session."""
    if factory_path := env.get(INTERVIEWER_FACTORY_ENV):
        # Subprocess tests need a deterministic interview outside src.
        # Keep that boundary explicit and narrow.
        factory = _load_interviewer_factory(factory_path)
        return factory(project_root, logger)

    config = runtime_config or load_agent_runtime_config(env)
    validate_agent_runtime_credentials(config, env)
    logger.write(
        "session_config",
        {
            "model_provider": config.model_provider,
            "model_preset": config.model_preset,
            "interviewer_model": config.models.interviewer,
            "explorer_model": config.models.explorer,
        },
    )
    return Interviewer(
        project_root=project_root,
        logger=logger,
        model_config=config.models,
    )


def validate_interviewer_configuration(env: Mapping[str, str]) -> None:
    """Validate the configured interviewer before project mutation."""
    if factory_path := env.get(INTERVIEWER_FACTORY_ENV):
        _load_interviewer_factory(factory_path)
        return
    config = load_agent_runtime_config(env)
    validate_agent_runtime_credentials(config, env)


def _load_interviewer_factory(path: str) -> InterviewerFactory:
    module_name, separator, function_name = path.partition(":")
    if not module_name or separator != ":" or not function_name:
        msg = (
            f"{INTERVIEWER_FACTORY_ENV} must be formatted as module:function."
        )
        raise ConfigError(msg)

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        msg = (
            f"{INTERVIEWER_FACTORY_ENV} could not import module "
            f"{module_name!r}."
        )
        raise ConfigError(msg) from exc
    candidate = getattr(module, function_name, None)
    if not callable(candidate):
        msg = f"{INTERVIEWER_FACTORY_ENV} does not point to a callable."
        raise ConfigError(msg)
    return cast("InterviewerFactory", candidate)


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
            yielded_event_count = len(events)
            finalized = await self._append_trigger_fallback_events(
                deps,
                events,
            )
            for event in events[yielded_event_count:]:
                yield event
            if finalized:
                return
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
            pending_text_events = self._coerce_raw_question_text_events(
                pending_text_events,
                deps,
            )
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
        outstanding_non_ask_tool_results = 0
        pending_stop_events: list[InterviewEvent] | None = None
        async with self.agent.run_stream_events(
            user_message,
            message_history=self._messages,
            deps=deps,
            instructions=turn_context,
        ) as stream:
            async for event in stream:
                waiting_for_tool_results = (
                    pending_stop_events is not None
                    and outstanding_non_ask_tool_results > 0
                )
                if waiting_for_tool_results and not isinstance(
                    event,
                    FunctionToolResultEvent,
                ):
                    continue
                if _is_non_ask_tool_call_event(event):
                    outstanding_non_ask_tool_results += 1
                visible_events, should_stop = self._record_stream_event(
                    event,
                    deps,
                    buffer,
                )
                if _is_non_ask_tool_result_event(event):
                    outstanding_non_ask_tool_results = max(
                        0,
                        outstanding_non_ask_tool_results - 1,
                    )
                if (
                    pending_stop_events is not None
                    and outstanding_non_ask_tool_results <= 0
                ):
                    if should_stop:
                        _queue_recorded_events(
                            event,
                            visible_events=visible_events,
                            queue=queue,
                            pending_text_events=pending_text_events,
                        )
                        pending_stop_events = None
                        break
                    _queue_events(queue, pending_stop_events)
                    pending_stop_events = None
                    break
                if (
                    should_stop
                    and _is_ask_tool_call_event(event)
                    and outstanding_non_ask_tool_results > 0
                ):
                    pending_text_events.clear()
                    pending_stop_events = visible_events
                    continue
                _queue_recorded_events(
                    event,
                    visible_events=visible_events,
                    queue=queue,
                    pending_text_events=pending_text_events,
                )
                if should_stop:
                    break
        if pending_stop_events is not None:
            _queue_events(queue, pending_stop_events)
        return pending_text_events

    def _record_stream_event(
        self,
        event: object,
        deps: InterviewerDeps,
        buffer: _TurnEventBuffer,
    ) -> tuple[list[InterviewEvent], bool]:
        if isinstance(event, FunctionToolCallEvent):
            return self._record_tool_call_event(event, deps, buffer)
        if isinstance(event, FunctionToolResultEvent):
            return self._record_tool_result_event(event, deps, buffer)
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
        deps: InterviewerDeps,
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
        question_event = InterviewEvent(kind="question", content=question)
        visible_events.append(question_event)
        buffer.events.append(question_event)
        self._append_question_to_history(
            question,
            user_message=deps.latest_user_message,
        )
        return visible_events, True

    def _record_tool_result_event(
        self,
        event: FunctionToolResultEvent,
        deps: InterviewerDeps,
        buffer: _TurnEventBuffer,
    ) -> tuple[list[InterviewEvent], bool]:
        self.logger.write(
            "model_tool_call_finished",
            _serialize_model_tool_result(event),
        )
        if not deps.finalized or not _is_finalize_tool_result_event(event):
            return [], False
        content = event.part.content
        self._should_exit = True
        if not isinstance(content, str):
            return [], True
        buffer.events = [
            item
            for item in buffer.events
            if item.kind not in {"text", "text_delta"}
        ]
        visible_event = InterviewEvent(kind="text", content=content)
        buffer.events.append(visible_event)
        return [visible_event], True

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
            question = _parse_raw_question_text(result.output)
            if question is not None and not deps.finalized:
                visible_events = [
                    InterviewEvent(kind="tool_call", content=_ASK_TOOL_NAME),
                    InterviewEvent(kind="question", content=question),
                ]
                buffer.events.extend(visible_events)
                self._append_question_to_history(question)
                return visible_events
            visible_event = InterviewEvent(kind="text", content=result.output)
            buffer.events.append(visible_event)
            return [visible_event]
        return []

    def _coerce_raw_question_text_events(
        self,
        events: list[InterviewEvent],
        deps: InterviewerDeps,
    ) -> list[InterviewEvent]:
        if deps.finalized:
            return events
        text = "".join(
            event.content
            for event in events
            if event.kind in {"text", "text_delta"}
            and isinstance(event.content, str)
        )
        question = _parse_raw_question_text(text)
        if question is None:
            return events
        self._append_question_to_history(question)
        return [
            InterviewEvent(kind="tool_call", content=_ASK_TOOL_NAME),
            InterviewEvent(kind="question", content=question),
        ]

    def _append_question_to_history(
        self,
        question: InterviewQuestion,
        *,
        user_message: str | None = None,
    ) -> None:
        current_turn_messages: list[ModelMessage] = []
        if user_message is not None:
            current_turn_messages.append(
                ModelRequest(parts=[UserPromptPart(user_message)])
            )
        self._messages = [
            *self._messages,
            *current_turn_messages,
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

        missing_readiness = _find_missing_persisted_mvp_readiness(
            deps.project_root
        )
        if missing_readiness:
            self.logger.write(
                "trigger_fallback_skipped",
                {
                    "reason": "missing_mvp_readiness_facts",
                    "missing": list(missing_readiness),
                },
            )
            question = InterviewQuestion(
                level="high",
                question=(
                    "What should we decide for "
                    f"{missing_readiness[0]} before Ralph starts?"
                ),
            )
            events.extend([
                InterviewEvent(
                    kind="text",
                    content=format_missing_mvp_readiness(missing_readiness),
                ),
                InterviewEvent(kind="question", content=question),
            ])
            self._append_question_to_history(question)
            return False

        readiness_summary = (
            "Explicit trigger received after persisted specs were captured."
        )
        result = await run_logged_tool(
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
    ) or (
        isinstance(event, FunctionToolResultEvent)
        and _is_finalize_tool_result_event(event)
    )


def _is_ask_tool_call_event(event: object) -> bool:
    return (
        isinstance(event, FunctionToolCallEvent)
        and event.part.tool_name == _ASK_TOOL_NAME
    )


def _is_non_ask_tool_call_event(event: object) -> bool:
    return (
        isinstance(event, FunctionToolCallEvent)
        and event.part.tool_name != _ASK_TOOL_NAME
    )


def _is_non_ask_tool_result_event(event: object) -> bool:
    return (
        isinstance(event, FunctionToolResultEvent)
        and isinstance(event.part, ToolReturnPart)
        and event.part.tool_name != _ASK_TOOL_NAME
    )


def _is_finalize_tool_result_event(event: FunctionToolResultEvent) -> bool:
    part = event.part
    return (
        isinstance(part, ToolReturnPart) and part.tool_name == "finalize_specs"
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
    _queue_events(queue, visible_events)


def _queue_events(
    queue: asyncio.Queue[object],
    events: list[InterviewEvent],
) -> None:
    for event in events:
        queue.put_nowait(event)


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


_RAW_QUESTION_PATTERN = re.compile(
    r"^Question \((?P<level>high|low)\): (?P<question>.+)$",
    re.IGNORECASE,
)


def _parse_raw_question_text(text: str) -> InterviewQuestion | None:
    lines = [line.strip() for line in text.strip().splitlines()]
    if not lines:
        return None
    match = _RAW_QUESTION_PATTERN.fullmatch(lines[0])
    if match is None:
        return None

    choices: list[AskChoice] = []
    default: str | None = None
    for line in lines[1:]:
        if not line:
            continue
        label, separator, body = line.partition(":")
        if not separator:
            return None
        normalized_label = label.strip().lower()
        if normalized_label == "choices":
            choices = _parse_raw_question_choices(body)
            continue
        if normalized_label == "default":
            default = body.strip() or None
            continue
        return None

    return build_question(
        level=cast("Literal['high', 'low']", match.group("level").lower()),
        question=match.group("question").strip(),
        choices=choices,
        default=default,
    )


def _parse_raw_question_choices(value: str) -> list[AskChoice]:
    choices: list[AskChoice] = []
    for raw_choice in value.split(";"):
        label, separator, description = raw_choice.strip().partition(" - ")
        if not label:
            continue
        choices.append(
            AskChoice(
                label=label,
                description=description if separator else None,
            )
        )
    return choices


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


def _find_missing_persisted_mvp_readiness(
    project_root: Path,
) -> tuple[str, ...]:
    return check_mvp_readiness(_read_persisted_specs(project_root)).missing


def _read_persisted_specs(project_root: Path) -> str:
    specs_dir = project_root / ".jri" / "specs"
    return "\n\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(specs_dir.glob("**/*.md"))
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
