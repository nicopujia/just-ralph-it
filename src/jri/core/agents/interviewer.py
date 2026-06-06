"""Project-intent interviewer agent."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    FunctionToolCallEvent,
    ModelMessage,
    PartDeltaEvent,
    RunContext,
    TextPartDelta,
    Tool,
)

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
from jri.core.tools.spec import write_spec


@dataclass
class InterviewerDeps:
    """Dependencies available to interviewer tools."""

    project_root: Path
    latest_user_message: str
    logger: JsonlLogger
    explorer: ContextExplorer
    finalized: bool = False


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
            tools=[
                Tool(write_spec_tool, takes_ctx=True, name="spec"),
                Tool(write_note_tool, takes_ctx=True, name="note"),
                Tool(ask_question_tool, takes_ctx=False, name="ask"),
                Tool(explore_tool, takes_ctx=True, name="explore"),
                Tool(finalize_tool, takes_ctx=True, name="just_ralph_it"),
            ],
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
        saw_text_delta = False
        events: list[InterviewEvent] = []
        async with self.agent.run_stream_events(
            user_message,
            message_history=self._messages,
            deps=deps,
            instructions=build_interviewer_context(self.project_root),
        ) as stream:
            async for event in stream:
                if isinstance(event, FunctionToolCallEvent):
                    events.append(
                        InterviewEvent(
                            kind="tool_call",
                            content=event.part.tool_name,
                        )
                    )
                    if event.part.tool_name == "ask":
                        events.append(
                            InterviewEvent(
                                kind="question",
                                content=_build_ask_tool_call_question(
                                    event.part.args
                                ),
                            ),
                        )
                        break
                elif isinstance(event, PartDeltaEvent) and isinstance(
                    event.delta,
                    TextPartDelta,
                ):
                    saw_text_delta = True
                    events.append(
                        InterviewEvent(
                            kind="text_delta",
                            content=event.delta.content_delta,
                        )
                    )
                elif isinstance(event, AgentRunResultEvent):
                    self._messages = event.result.all_messages()[-12:]
                    self._should_exit = deps.finalized
                    if not saw_text_delta:
                        events.append(
                            InterviewEvent(
                                kind="text",
                                content=event.result.output,
                            )
                        )
        for event in events:
            yield event


async def write_spec_tool(
    ctx: RunContext[InterviewerDeps],
    path: str,
    content: str | None = None,
    patch_text: str | None = None,
) -> str:
    """Create, replace, or patch a curated project spec file."""
    return await _run_logged_tool(
        ctx.deps,
        "spec",
        {"path": path, "mode": "patch" if patch_text is not None else "write"},
        lambda: write_spec(
            project_root=ctx.deps.project_root,
            path=path,
            content=content,
            patch_text=patch_text,
        ),
    )


async def write_note_tool(
    ctx: RunContext[InterviewerDeps],
    content: str | None = None,
    patch_text: str | None = None,
) -> str:
    """Create, replace, or patch the interviewer scratchpad."""
    return await _run_logged_tool(
        ctx.deps,
        "note",
        {"mode": "patch" if patch_text is not None else "write"},
        lambda: write_note(
            project_root=ctx.deps.project_root,
            content=content,
            patch_text=patch_text,
        ),
    )


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
    spec_path: str = "product",
    known_blockers: list[str] | None = None,
) -> str:
    """Persist the final spec, commit JRI files, and exit."""
    blocker_log: list[JsonValue] = list(known_blockers or [])

    async def finalize() -> str:
        if not spec_content.strip():
            msg = "just_ralph_it requires non-empty final spec content."
            raise JustRalphItError(msg)
        if not known_blockers:
            await write_spec(
                project_root=ctx.deps.project_root,
                path=spec_path,
                content=spec_content,
            )
        result = await finalize_jri(
            project_root=ctx.deps.project_root,
            latest_user_message=ctx.deps.latest_user_message,
            readiness_summary=readiness_summary,
            known_blockers=known_blockers,
        )
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
    deps.logger.write(
        "tool_call_started",
        {"tool_name": tool_name, "arguments": dict(arguments)},
    )
    try:
        result = await action()
    except Exception as exc:
        deps.logger.write(
            "tool_call_failed",
            {"tool_name": tool_name, "error": str(exc)},
        )
        raise
    deps.logger.write(
        "tool_call_finished",
        {"tool_name": tool_name, "result": result},
    )
    return result
