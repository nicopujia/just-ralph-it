# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportInvalidCast=false, reportUnknownMemberType=false
"""Tests for the live interviewer adapter."""

import asyncio
import contextlib
import json
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Self, cast, override

import pytest
from pydantic_ai import (
    AgentRunResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRetry,
    PartDeltaEvent,
    PartStartEvent,
    RunContext,
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
from pydantic_ai.usage import RequestUsage, RunUsage

from jri.core.agents.interviewer import (
    INTERVIEWER_FACTORY_ENV,
    Interviewer,
    InterviewerDeps,
    create_interviewer,
    validate_interviewer_configuration,
)
from jri.core.agents.models import AgentModelConfig
from jri.core.interview import (
    InterviewEvent,
    InterviewQuestion,
    QuestionChoice,
)
from jri.core.logging import JsonlLogger
from jri.core.tools.interviewer import (
    ask_question_tool,
    build_interviewer_tools,
    explore_context_tool,
    finalize_specs_tool,
    record_notes_tool,
    update_scratchpad_tool,
    update_specs_tool,
    write_note_tool,
    write_spec_tool,
)
from jri.core.tools.just_ralph_it import JustRalphItError
from tests.doubles.agents import (
    FakeRunContext,
    FakeRunResult,
    FakeStream,
    FakeStreamAgent,
)
from tests.doubles.explorers import RecordingExplorer


class StableRepr:
    """Object with stable repr for trace conversion tests."""

    @override
    def __repr__(self) -> str:
        """Return deterministic debug text."""
        return "stable-repr"


def test_interviewer_module_exports_construction_helpers() -> None:
    """Interviewer construction helpers live with the interviewer."""
    assert INTERVIEWER_FACTORY_ENV == "JRI_INTERVIEWER_FACTORY"
    assert create_interviewer.__module__ == "jri.core.agents.interviewer"
    assert (
        validate_interviewer_configuration.__module__
        == "jri.core.agents.interviewer"
    )


class FinalizedStreamAgent(FakeStreamAgent):
    """Fake stream agent that marks deps finalized before yielding events."""

    @override
    def run_stream_events(
        self,
        *args: object,
        **kwargs: object,
    ) -> FakeStream:
        """Mark the injected deps as finalized."""
        deps = cast("InterviewerDeps", kwargs["deps"])
        deps.finalized = True
        return super().run_stream_events(*args, **kwargs)


class FailingStreamAgent:
    """Fake stream agent that raises before yielding model events."""

    instructions: str = ""

    def run_stream_events(
        self,
        *args: object,
        **kwargs: object,
    ) -> "FailingStream":
        """Return a stream that raises an unexpected model error."""
        _ = args
        self.instructions = str(kwargs["instructions"])
        return FailingStream()


class FailingStream:
    """Async context manager that raises on entry."""

    async def __aenter__(self) -> object:
        msg = "tool exceeded max retries"
        raise UnexpectedModelBehavior(msg)

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        _ = (exc_type, exc, traceback)


class ErrorAfterRunResultAgent:
    """Fake stream agent that errors after a visible run result."""

    def run_stream_events(
        self,
        *args: object,
        **kwargs: object,
    ) -> "ErrorAfterRunResultStream":
        """Return a stream that emits readiness text, then errors."""
        _ = (args, kwargs)
        return ErrorAfterRunResultStream()


class ErrorAfterRunResultStream:
    """Async context manager that raises after a run result event."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        _ = (exc_type, exc, traceback)

    def __aiter__(self) -> AsyncIterator[object]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[object]:
        yield AgentRunResultEvent(result=FakeRunResult("Ready to hand off."))
        msg = "tool exceeded max retries"
        raise UnexpectedModelBehavior(msg)


class BlockingAfterFirstTextStreamAgent:
    """Fake stream agent that pauses after one provider text event."""

    def __init__(self) -> None:
        self.instructions: str = ""
        self.message_history: list[object] = []
        self.first_provider_event_seen: asyncio.Event = asyncio.Event()
        self.allow_completion: asyncio.Event = asyncio.Event()
        self.stream_completed: asyncio.Event = asyncio.Event()

    def run_stream_events(
        self,
        *args: object,
        **kwargs: object,
    ) -> "BlockingAfterFirstTextStream":
        """Return a controlled stream and record run instructions."""
        _ = args
        self.instructions = str(kwargs["instructions"])
        self.message_history = list(
            cast("list[object]", kwargs["message_history"])
        )
        return BlockingAfterFirstTextStream(self)


class BlockingAfterFirstTextStream:
    """Async context manager that blocks provider completion."""

    def __init__(self, agent: BlockingAfterFirstTextStreamAgent) -> None:
        self.agent: BlockingAfterFirstTextStreamAgent = agent

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        _ = (exc_type, exc, traceback)

    def __aiter__(self) -> AsyncIterator[object]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[object]:
        self.agent.first_provider_event_seen.set()
        yield PartStartEvent(index=0, part=TextPart("I'm"))
        await self.agent.allow_completion.wait()
        yield AgentRunResultEvent(result=FakeRunResult("ignored"))
        self.agent.stream_completed.set()


class BlockingBeforeAskStreamAgent:
    """Fake stream agent that pauses between preamble text and ask."""

    def __init__(self) -> None:
        self.instructions: str = ""
        self.message_history: list[object] = []
        self.first_provider_event_seen: asyncio.Event = asyncio.Event()
        self.allow_ask: asyncio.Event = asyncio.Event()

    def run_stream_events(
        self,
        *args: object,
        **kwargs: object,
    ) -> "BlockingBeforeAskStream":
        """Return a controlled stream and record run instructions."""
        _ = args
        self.instructions = str(kwargs["instructions"])
        self.message_history = list(
            cast("list[object]", kwargs["message_history"])
        )
        return BlockingBeforeAskStream(self)


class BlockingBeforeAskStream:
    """Async context manager that delays an ask after text."""

    def __init__(self, agent: BlockingBeforeAskStreamAgent) -> None:
        self.agent: BlockingBeforeAskStreamAgent = agent

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        _ = (exc_type, exc, traceback)

    def __aiter__(self) -> AsyncIterator[object]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[object]:
        self.agent.first_provider_event_seen.set()
        yield PartStartEvent(index=0, part=TextPart("Preamble."))
        await self.agent.allow_ask.wait()
        yield FunctionToolCallEvent(
            ToolCallPart(
                "ask_question",
                args='{"level":"high","question":"Who is this for?"}',
            )
        )


class BlockingAfterToolCallStreamAgent:
    """Fake stream agent that blocks after a visible tool call."""

    def __init__(self) -> None:
        self.instructions: str = ""
        self.message_history: list[object] = []
        self.stream_closed: asyncio.Event = asyncio.Event()
        self.never_finish: asyncio.Event = asyncio.Event()

    def run_stream_events(
        self,
        *args: object,
        **kwargs: object,
    ) -> "BlockingAfterToolCallStream":
        """Return a controlled stream and record run instructions."""
        _ = args
        self.instructions = str(kwargs["instructions"])
        self.message_history = list(
            cast("list[object]", kwargs["message_history"])
        )
        return BlockingAfterToolCallStream(self)


class BlockingAfterToolCallStream:
    """Async context manager that keeps the provider stream open."""

    def __init__(self, agent: BlockingAfterToolCallStreamAgent) -> None:
        self.agent: BlockingAfterToolCallStreamAgent = agent

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        _ = (exc_type, exc, traceback)

    def __aiter__(self) -> AsyncIterator[object]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[object]:
        try:
            yield FunctionToolCallEvent(ToolCallPart("update_specs"))
            await self.agent.never_finish.wait()
        finally:
            self.agent.stream_closed.set()


@dataclass(frozen=True)
class DelayedToolResultAfterAskConfig:
    """Configuration for delayed post-ask tool result streams."""

    event_after_ask: object | None = None
    finalizes: bool = False
    result_content: object = "Scratchpad updated."
    tool_call_id: str = "call-notes"
    tool_name: str = "update_scratchpad"
    yield_tool_result: bool = True


class DelayedToolResultAfterAskStreamAgent:
    """Fake stream where an earlier tool result arrives after an ask."""

    def __init__(
        self,
        project_root: Path,
        *,
        config: DelayedToolResultAfterAskConfig | None = None,
    ) -> None:
        resolved_config = config or DelayedToolResultAfterAskConfig()
        self.project_root: Path = project_root
        self.event_after_ask: object | None = resolved_config.event_after_ask
        self.finalizes: bool = resolved_config.finalizes
        self.instructions: str = ""
        self.message_history: list[object] = []
        self.result_content: object = resolved_config.result_content
        self.allow_tool_result: asyncio.Event = asyncio.Event()
        self.tool_call_id: str = resolved_config.tool_call_id
        self.tool_name: str = resolved_config.tool_name
        self.yield_tool_result: bool = resolved_config.yield_tool_result

    def run_stream_events(
        self,
        *args: object,
        **kwargs: object,
    ) -> "DelayedToolResultAfterAskStream":
        """Return a controlled stream and record run instructions."""
        _ = args
        deps = cast("InterviewerDeps", kwargs["deps"])
        if self.finalizes:
            deps.finalized = True
        self.instructions = str(kwargs["instructions"])
        self.message_history = list(
            cast("list[object]", kwargs["message_history"])
        )
        return DelayedToolResultAfterAskStream(self)


class DelayedToolResultAfterAskStream:
    """Async context manager for delayed post-ask tool results."""

    def __init__(self, agent: DelayedToolResultAfterAskStreamAgent) -> None:
        self.agent: DelayedToolResultAfterAskStreamAgent = agent

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        _ = (exc_type, exc, traceback)

    def __aiter__(self) -> AsyncIterator[object]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[object]:
        yield FunctionToolCallEvent(
            ToolCallPart(
                self.agent.tool_name,
                tool_call_id=self.agent.tool_call_id,
            )
        )
        yield FunctionToolCallEvent(
            ToolCallPart(
                "ask_question",
                args='{"level":"high","question":"Who is this for?"}',
                tool_call_id="call-ask",
            )
        )
        if self.agent.event_after_ask is not None:
            yield self.agent.event_after_ask
        if not self.agent.yield_tool_result:
            return
        await self.agent.allow_tool_result.wait()
        if self.agent.tool_name == "update_scratchpad":
            scratchpad = self.agent.project_root / ".jri" / "scratchpad.md"
            scratchpad.parent.mkdir(parents=True, exist_ok=True)
            scratchpad.write_text("Captured target user.\n", encoding="utf-8")
        yield FunctionToolResultEvent(
            ToolReturnPart(
                tool_name=self.agent.tool_name,
                content=self.agent.result_content,
                tool_call_id=self.agent.tool_call_id,
            )
        )


def test_interviewer_streams_tool_and_text_events(
    tmp_path: Path,
) -> None:
    """Interviewer converts Pydantic AI events to REPL events."""
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    fake_agent = FakeStreamAgent([
        FunctionToolCallEvent(ToolCallPart("update_specs")),
        PartDeltaEvent(index=0, delta=TextPartDelta("hello")),
        AgentRunResultEvent(result=FakeRunResult("ignored")),
    ])
    object.__setattr__(interviewer, "agent", fake_agent)

    events = asyncio.run(_collect(interviewer.respond("idea")))

    assert not interviewer.should_exit
    assert [(event.kind, event.content) for event in events] == [
        ("tool_call", "update_specs"),
        ("text_delta", "hello"),
    ]
    assert fake_agent.instructions


def test_interviewer_joins_text_part_start_and_delta_chunks(
    tmp_path: Path,
) -> None:
    """Interviewer exposes text that arrives in a part start event."""
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([
            PartStartEvent(index=0, part=TextPart("I'm")),
            PartDeltaEvent(index=0, delta=TextPartDelta(" here to help.")),
            AgentRunResultEvent(result=FakeRunResult("ignored")),
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("idea")))

    assert (
        "".join(
            cast("str", event.content)
            for event in events
            if event.kind in {"text", "text_delta"}
        )
        == "I'm here to help."
    )


def test_interviewer_holds_text_until_stream_completion(
    tmp_path: Path,
) -> None:
    """Interviewer holds text until it knows no question will replace it."""
    first_event_was_progressive, first_event = asyncio.run(
        _receive_first_event_before_stream_completion(tmp_path)
    )

    assert not first_event_was_progressive
    assert (first_event.kind, first_event.content) == ("text_delta", "I'm")


def test_interviewer_does_not_leak_delayed_preamble_before_question(
    tmp_path: Path,
) -> None:
    """Text that precedes a delayed ask tool call is not user-visible."""
    first_event_was_waiting, events = asyncio.run(
        _collect_delayed_ask_events(tmp_path)
    )

    assert first_event_was_waiting
    assert [(event.kind, event.content) for event in events] == [
        ("tool_call", "ask_question"),
        (
            "question",
            InterviewQuestion(level="high", question="Who is this for?"),
        ),
    ]


def test_interviewer_cancels_provider_when_response_iterator_closes(
    tmp_path: Path,
) -> None:
    """Closing the response iterator cancels the provider stream task."""
    first_event, stream_closed = asyncio.run(
        _close_iterator_after_first_event(tmp_path)
    )

    assert (first_event.kind, first_event.content) == (
        "tool_call",
        "update_specs",
    )
    assert stream_closed


def test_interviewer_waits_for_prior_tool_result_before_question(
    tmp_path: Path,
) -> None:
    """Post-ask write results are consumed before yielding the question."""
    question_was_visible_early, events = asyncio.run(
        _collect_delayed_tool_result_after_ask(tmp_path)
    )

    assert not question_was_visible_early
    assert [(event.kind, event.content) for event in events] == [
        ("tool_call", "update_scratchpad"),
        ("tool_call", "ask_question"),
        (
            "question",
            InterviewQuestion(level="high", question="Who is this for?"),
        ),
    ]
    scratchpad = tmp_path / ".jri" / "scratchpad.md"
    assert scratchpad.read_text(encoding="utf-8") == (
        "Captured target user.\n"
    )
    tool_result = _event_by_call_id(
        _read_events(tmp_path / "events.jsonl"),
        event_type="model_tool_call_finished",
        tool_call_id="call-notes",
    )
    assert cast("dict[str, object]", tool_result["data"])["tool_name"] == (
        "update_scratchpad"
    )


def test_interviewer_ignores_post_ask_noise_while_waiting_for_tool_result(
    tmp_path: Path,
) -> None:
    """Buffered asks suppress later text until prior writes finish."""
    fake_agent = DelayedToolResultAfterAskStreamAgent(
        tmp_path,
        config=DelayedToolResultAfterAskConfig(
            event_after_ask=PartDeltaEvent(
                index=0,
                delta=TextPartDelta("Late prose."),
            ),
        ),
    )
    question_was_visible_early, events = asyncio.run(
        _collect_delayed_tool_result_after_ask(
            tmp_path,
            fake_agent=fake_agent,
        )
    )

    assert not question_was_visible_early
    assert [(event.kind, event.content) for event in events] == [
        ("tool_call", "update_scratchpad"),
        ("tool_call", "ask_question"),
        (
            "question",
            InterviewQuestion(level="high", question="Who is this for?"),
        ),
    ]


def test_interviewer_yields_buffered_question_if_tool_result_never_arrives(
    tmp_path: Path,
) -> None:
    """A closed provider stream still releases the buffered question."""
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        DelayedToolResultAfterAskStreamAgent(
            tmp_path,
            config=DelayedToolResultAfterAskConfig(yield_tool_result=False),
        ),
    )

    events = asyncio.run(_collect(interviewer.respond("idea")))

    assert [(event.kind, event.content) for event in events] == [
        ("tool_call", "update_scratchpad"),
        ("tool_call", "ask_question"),
        (
            "question",
            InterviewQuestion(level="high", question="Who is this for?"),
        ),
    ]


def test_interviewer_preserves_delayed_finalization_after_buffered_ask(
    tmp_path: Path,
) -> None:
    """A finalization result still wins over a buffered ask question."""
    final_message = "Specs finalized and committed."
    fake_agent = DelayedToolResultAfterAskStreamAgent(
        tmp_path,
        config=DelayedToolResultAfterAskConfig(
            finalizes=True,
            result_content=final_message,
            tool_call_id="call-finalize",
            tool_name="finalize_specs",
        ),
    )

    question_was_visible_early, events = asyncio.run(
        _collect_delayed_tool_result_after_ask(
            tmp_path,
            fake_agent=fake_agent,
        )
    )

    assert not question_was_visible_early
    assert [(event.kind, event.content) for event in events] == [
        ("tool_call", "finalize_specs"),
        ("text", final_message),
    ]


def test_interviewer_logs_raw_model_tool_calls_and_results(
    tmp_path: Path,
) -> None:
    """Model stream logs include raw tool args and returned content."""
    log_path = tmp_path / "events.jsonl"
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(log_path),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([
            FunctionToolCallEvent(
                ToolCallPart(
                    "update_specs",
                    args={"spec_name": "product", "content": "# Product"},
                    tool_call_id="call-1",
                )
            ),
            FunctionToolCallEvent(
                ToolCallPart(
                    "record_notes",
                    args={"items": ["value", ("tuple", StableRepr())]},
                    tool_call_id="call-2",
                )
            ),
            FunctionToolResultEvent(
                ToolReturnPart(
                    tool_name="update_specs",
                    content="retry",
                    tool_call_id="call-1",
                )
            ),
            AgentRunResultEvent(
                result=FakeRunResult(
                    "done",
                    usage=RunUsage(
                        input_tokens=10,
                        output_tokens=4,
                        requests=1,
                        tool_calls=2,
                    ),
                    response=ModelResponse(
                        parts=[TextPart("done")],
                        usage=RequestUsage(
                            input_tokens=5,
                            output_tokens=2,
                        ),
                        model_name="test-model",
                        provider_name="test-provider",
                        provider_response_id="response-1",
                        finish_reason="stop",
                    ),
                )
            ),
        ]),
    )

    _ = asyncio.run(_collect(interviewer.respond("idea")))

    events = _read_events(log_path)
    tool_call = _event_by_call_id(
        events,
        event_type="model_tool_call_started",
        tool_call_id="call-1",
    )
    complex_tool_call = _event_by_call_id(
        events,
        event_type="model_tool_call_started",
        tool_call_id="call-2",
    )
    tool_result = _latest_event(events, "model_tool_call_finished")
    turn_finished = _latest_event(events, "model_turn_finished")
    assert tool_call["data"] == {
        "tool_name": "update_specs",
        "tool_call_id": "call-1",
        "args": {"spec_name": "product", "content": "# Product"},
        "args_json": '{"spec_name":"product","content":"# Product"}',
    }
    assert tool_result["data"] == {
        "tool_name": "update_specs",
        "tool_call_id": "call-1",
        "part_kind": "tool-return",
        "content": "retry",
    }
    assert cast("dict[str, object]", complex_tool_call["data"])["args"] == {
        "items": ["value", ["tuple", "stable-repr"]]
    }
    turn_data = cast("dict[str, object]", turn_finished["data"])
    response = cast("dict[str, object]", turn_data["response"])
    usage = cast("dict[str, object]", turn_data["usage"])
    assert turn_data["output"] == "done"
    assert usage["tool_calls"] == 2
    assert response["provider_response_id"] == "response-1"
    assert response["part_kinds"] == ["text"]


def test_interviewer_registers_high_level_model_tools(
    tmp_path: Path,
) -> None:
    """Provider schemas expose verbs without storage or patch mechanics."""
    _ = tmp_path
    tools = {tool.name: tool for tool in build_interviewer_tools()}

    assert set(tools) == {
        "ask_question",
        "update_scratchpad",
        "update_specs",
        "explore_context",
        "finalize_specs",
    }
    assert tools["update_specs"].strict is True
    assert tools["update_specs"].max_retries == 3
    assert tools["update_scratchpad"].strict is True
    assert tools["update_scratchpad"].max_retries == 3
    assert tools["finalize_specs"].strict is True
    assert tools["finalize_specs"].max_retries == 3
    assert tools["update_specs"].function_schema.json_schema["required"] == [
        "spec_name",
        "content",
    ]
    assert tools["update_scratchpad"].function_schema.json_schema[
        "required"
    ] == ["notes"]
    assert tools["finalize_specs"].function_schema.json_schema["required"] == [
        "readiness_summary",
        "spec_content",
    ]
    exposed_text = "\n".join(
        "\n".join([
            str(tool.description),
            json.dumps(tool.function_schema.json_schema, sort_keys=True),
        ])
        for tool in tools.values()
    ).lower()
    for internal_term in [
        ".jri",
        ".jri/specs",
        "absolute path",
        "patch_text",
        "patch envelope",
        "*** begin patch",
        "*** end patch",
    ]:
        assert internal_term not in exposed_text


def test_interviewer_yields_final_output_without_text_delta(
    tmp_path: Path,
) -> None:
    """Interviewer yields final output if no text deltas arrive."""
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([AgentRunResultEvent(result=FakeRunResult("done"))]),
    )

    events = asyncio.run(_collect(interviewer.respond("idea")))

    assert [(event.kind, event.content) for event in events] == [
        ("text", "done")
    ]


def test_interviewer_finalizes_persisted_specs_on_trigger_fallback(
    tmp_path: Path,
) -> None:
    """Explicit trigger finalizes persisted specs if the model only talks."""
    _configure_repo(tmp_path)
    log_path = tmp_path / ".jri" / "logs" / "interview.jsonl"
    (tmp_path / ".jri" / "logs").mkdir(parents=True)
    (tmp_path / ".jri" / ".gitignore").write_text("logs/\n")
    specs = tmp_path / ".jri" / "specs"
    specs.mkdir(parents=True)
    (specs / "product.md").write_text(
        _complete_spec(),
        encoding="utf-8",
    )
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(log_path),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([
            AgentRunResultEvent(result=FakeRunResult("Ready to hand off."))
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("just ralph it")))

    assert [(event.kind, event.content) for event in events] == [
        ("text", "Ready to hand off."),
        ("tool_call", "finalize_specs"),
        (
            "text",
            (
                "Specs finalized and committed. Ralph is coming soon to JRI. "
                "For now, you need to figure out how to implement the specs "
                "yourself. Readiness: "
                "Explicit trigger received after persisted specs were "
                "captured."
            ),
        ),
    ]
    assert interviewer.should_exit
    assert _has_commit(tmp_path)
    started = _latest_event(_read_events(log_path), "tool_call_started")
    assert cast("dict[str, object]", started["data"])["arguments"] == {
        "source": "trigger_fallback",
        "readiness_summary": (
            "Explicit trigger received after persisted specs were captured."
        ),
        "known_blockers": [],
    }


def test_interviewer_trigger_fallback_requires_readiness_signal(
    tmp_path: Path,
) -> None:
    """Blocker prose on a trigger does not finalize a persisted draft spec."""
    _configure_repo(tmp_path)
    log_path = tmp_path / ".jri" / "logs" / "interview.jsonl"
    (tmp_path / ".jri" / "logs").mkdir(parents=True)
    (tmp_path / ".jri" / ".gitignore").write_text("logs/\n")
    specs = tmp_path / ".jri" / "specs"
    specs.mkdir(parents=True)
    (specs / "product.md").write_text("# Draft\n", encoding="utf-8")
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(log_path),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([
            AgentRunResultEvent(
                result=FakeRunResult("I still need the target user.")
            )
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("just ralph it")))

    assert [(event.kind, event.content) for event in events] == [
        ("text", "I still need the target user.")
    ]
    assert not interviewer.should_exit
    assert not _has_commit(tmp_path)
    skipped = _latest_event(_read_events(log_path), "trigger_fallback_skipped")
    assert cast("dict[str, object]", skipped["data"])["reason"] == (
        "no_readiness_signal"
    )


def test_interviewer_trigger_fallback_rejects_incomplete_specs(
    tmp_path: Path,
) -> None:
    """Trigger fallback explains missing readiness facts."""
    _configure_repo(tmp_path)
    log_path = tmp_path / ".jri" / "logs" / "interview.jsonl"
    (tmp_path / ".jri" / "logs").mkdir(parents=True)
    (tmp_path / ".jri" / ".gitignore").write_text("logs/\n")
    specs = tmp_path / ".jri" / "specs"
    specs.mkdir(parents=True)
    (specs / "product.md").write_text(
        "# Product\n\n## Goal\n\nBuild a tiny CLI.\n",
        encoding="utf-8",
    )
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(log_path),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([
            AgentRunResultEvent(result=FakeRunResult("Ready to hand off."))
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("just ralph it")))

    assert [(event.kind, event.content) for event in events[:2]] == [
        ("text", "Ready to hand off."),
        (
            "text",
            (
                "Missing MVP readiness facts:\n"
                "- target user\n"
                "- workflows\n"
                "- inputs\n"
                "- outputs\n"
                "- persistence\n"
                "- integrations\n"
                "- errors\n"
                "- edge cases\n"
                "- non-goals\n"
                "- success criteria\n"
                "Please answer these before Ralph starts."
            ),
        ),
    ]
    assert events[2].content == InterviewQuestion(
        level="high",
        question=(
            "What should we decide for target user before Ralph starts?"
        ),
    )
    assert not interviewer.should_exit
    assert not _has_commit(tmp_path)
    skipped = _latest_event(_read_events(log_path), "trigger_fallback_skipped")
    assert cast("dict[str, object]", skipped["data"])["reason"] == (
        "missing_mvp_readiness_facts"
    )


def test_interviewer_trigger_fallback_defers_to_model_questions(
    tmp_path: Path,
) -> None:
    """A trigger-turn question means the model is still blocking handoff."""
    _configure_repo(tmp_path)
    (tmp_path / ".jri" / "logs").mkdir(parents=True)
    (tmp_path / ".jri" / ".gitignore").write_text("logs/\n")
    specs = tmp_path / ".jri" / "specs"
    specs.mkdir(parents=True)
    (specs / "product.md").write_text("# Product\n", encoding="utf-8")
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / ".jri" / "logs" / "interview.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([
            FunctionToolCallEvent(
                ToolCallPart(
                    "ask_question",
                    args='{"level":"high","question":"Who is this for?"}',
                )
            )
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("just ralph it")))

    assert [event.kind for event in events] == ["tool_call", "question"]
    assert not interviewer.should_exit
    assert not _has_commit(tmp_path)


def test_interviewer_trigger_fallback_rejects_model_errors_without_readiness(
    tmp_path: Path,
) -> None:
    """A model error without readiness does not finalize persisted specs."""
    _configure_repo(tmp_path)
    log_path = tmp_path / ".jri" / "logs" / "interview.jsonl"
    (tmp_path / ".jri" / "logs").mkdir(parents=True)
    (tmp_path / ".jri" / ".gitignore").write_text("logs/\n")
    specs = tmp_path / ".jri" / "specs"
    specs.mkdir(parents=True)
    (specs / "product.md").write_text("# Product\n", encoding="utf-8")
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(log_path),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(interviewer, "agent", FailingStreamAgent())

    with pytest.raises(UnexpectedModelBehavior, match="max retries"):
        asyncio.run(_collect(interviewer.respond("just ralph it")))

    assert not interviewer.should_exit
    assert not _has_commit(tmp_path)
    skipped = _latest_event(_read_events(log_path), "trigger_fallback_skipped")
    assert cast("dict[str, object]", skipped["data"])["reason"] == (
        "no_readiness_signal"
    )


def test_interviewer_exits_after_trigger_fallback_finalizes_on_model_error(
    tmp_path: Path,
) -> None:
    """A successful trigger fallback is visible even if the model errors."""
    _configure_repo(tmp_path)
    log_path = tmp_path / ".jri" / "logs" / "interview.jsonl"
    (tmp_path / ".jri" / "logs").mkdir(parents=True)
    (tmp_path / ".jri" / ".gitignore").write_text("logs/\n")
    specs = tmp_path / ".jri" / "specs"
    specs.mkdir(parents=True)
    (specs / "product.md").write_text(
        _complete_spec(),
        encoding="utf-8",
    )
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(log_path),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(interviewer, "agent", ErrorAfterRunResultAgent())

    events = asyncio.run(_collect(interviewer.respond("just ralph it")))

    assert [(event.kind, event.content) for event in events[:2]] == [
        ("text", "Ready to hand off."),
        ("tool_call", "finalize_specs"),
    ]
    assert "Ralph is coming soon to JRI" in cast("str", events[2].content)
    assert interviewer.should_exit
    assert _has_commit(tmp_path)


def test_interviewer_reraises_model_errors_without_fallback(
    tmp_path: Path,
) -> None:
    """Model errors still propagate when trigger fallback is not allowed."""
    log_path = tmp_path / "events.jsonl"
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(log_path),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(interviewer, "agent", FailingStreamAgent())

    with pytest.raises(UnexpectedModelBehavior, match="max retries"):
        asyncio.run(_collect(interviewer.respond("just ralph it")))

    skipped = _latest_event(_read_events(log_path), "trigger_fallback_skipped")
    assert cast("dict[str, object]", skipped["data"])["reason"] == (
        "no_persisted_spec"
    )


def test_interviewer_trigger_fallback_skips_without_persisted_specs(
    tmp_path: Path,
) -> None:
    """The fallback cannot finalize an empty interview."""
    log_path = tmp_path / "events.jsonl"
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(log_path),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([
            AgentRunResultEvent(result=FakeRunResult("Need more detail."))
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("just ralph it")))

    assert [(event.kind, event.content) for event in events] == [
        ("text", "Need more detail.")
    ]
    assert not interviewer.should_exit
    skipped = _latest_event(_read_events(log_path), "trigger_fallback_skipped")
    assert cast("dict[str, object]", skipped["data"])["reason"] == (
        "no_persisted_spec"
    )


def test_interviewer_trigger_fallback_skips_after_model_finalizes(
    tmp_path: Path,
) -> None:
    """Fallback does not run when the model already finalized."""
    log_path = tmp_path / "events.jsonl"
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(log_path),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FinalizedStreamAgent([
            AgentRunResultEvent(result=FakeRunResult("Finalized."))
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("just ralph it")))

    assert [(event.kind, event.content) for event in events] == [
        ("text", "Finalized.")
    ]
    assert interviewer.should_exit
    skipped = _latest_event(_read_events(log_path), "trigger_fallback_skipped")
    assert cast("dict[str, object]", skipped["data"])["reason"] == (
        "already_finalized"
    )


def test_interviewer_suppresses_model_prose_after_finalization_tool(
    tmp_path: Path,
) -> None:
    """Successful finalization shows the tool result, not later prose."""
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    final_message = (
        "Specs finalized and committed. Ralph is coming soon to JRI."
    )
    object.__setattr__(
        interviewer,
        "agent",
        FinalizedStreamAgent([
            FunctionToolCallEvent(ToolCallPart("finalize_specs")),
            FunctionToolResultEvent(
                ToolReturnPart(
                    tool_name="finalize_specs",
                    content=final_message,
                    tool_call_id="call-1",
                )
            ),
            AgentRunResultEvent(
                result=FakeRunResult("I can help implement this next.")
            ),
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("just ralph it")))

    assert [(event.kind, event.content) for event in events] == [
        ("tool_call", "finalize_specs"),
        ("text", final_message),
    ]
    assert interviewer.should_exit


def test_interviewer_exits_after_non_text_finalization_tool_result(
    tmp_path: Path,
) -> None:
    """Finalization stops the turn even when provider wraps the result."""
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FinalizedStreamAgent([
            FunctionToolCallEvent(ToolCallPart("finalize_specs")),
            FunctionToolResultEvent(
                ToolReturnPart(
                    tool_name="finalize_specs",
                    content={"message": "done"},
                    tool_call_id="call-1",
                )
            ),
            AgentRunResultEvent(result=FakeRunResult("ignored")),
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("just ralph it")))

    assert [(event.kind, event.content) for event in events] == [
        ("tool_call", "finalize_specs")
    ]
    assert interviewer.should_exit


def test_interviewer_ignores_unknown_stream_events(tmp_path: Path) -> None:
    """Unknown Pydantic AI stream events do not become REPL events."""
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([
            object(),
            AgentRunResultEvent(result=FakeRunResult("done")),
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("idea")))

    assert [(event.kind, event.content) for event in events] == [
        ("text", "done")
    ]


def test_interviewer_yields_ask_tool_call_question(
    tmp_path: Path,
) -> None:
    """Ask tool calls yield structured questions and end the turn."""
    log_path = tmp_path / "events.jsonl"
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(log_path),
        model_config=AgentModelConfig("test", "test"),
    )
    ask_args = (
        '{"level":"high","question":"Who is this for?",'
        '"choices":null,"default":null}'
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([
            PartDeltaEvent(index=0, delta=TextPartDelta("Preamble.")),
            FunctionToolCallEvent(ToolCallPart("ask_question", args=ask_args)),
            AgentRunResultEvent(result=FakeRunResult("ignored")),
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("idea")))

    assert [(event.kind, event.content) for event in events] == [
        ("tool_call", "ask_question"),
        (
            "question",
            InterviewQuestion(level="high", question="Who is this for?"),
        ),
    ]
    assert _latest_event(_read_events(log_path), "model_text_delta")[
        "data"
    ] == {"content": "Preamble."}


def test_interviewer_explores_user_supplied_urls_before_asking(
    tmp_path: Path,
) -> None:
    """URL context is gathered before the first follow-up question."""
    log_path = tmp_path / "events.jsonl"
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(log_path),
        model_config=AgentModelConfig("test", "test"),
    )
    explorer_output = (
        "Summary:\n"
        + "- Tripos is a game similar to ta-te-ti.\n\n"
        + "Sources:\n"
        + "- https://noticias.ulp.edu.ar/ciencia/tripos-5084"
    )
    explorer = RecordingExplorer(explorer_output)
    object.__setattr__(interviewer, "explorer", explorer)
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([
            FunctionToolCallEvent(
                ToolCallPart(
                    "ask_question",
                    args='{"level":"high","question":"Who uses Tripos?"}',
                )
            )
        ]),
    )

    events = asyncio.run(
        _collect(
            interviewer.respond(
                "tripos (see https://noticias.ulp.edu.ar/ciencia/tripos-5084)"
            )
        )
    )

    assert [(event.kind, event.content) for event in events] == [
        ("tool_call", "explore_context"),
        ("tool_call", "ask_question"),
        (
            "question",
            InterviewQuestion(level="high", question="Who uses Tripos?"),
        ),
    ]
    assert explorer.requests == [
        (
            tmp_path,
            (
                "Inspect the URL(s) from the latest user message before "
                "asking follow-up product questions.\n"
                "URL(s): https://noticias.ulp.edu.ar/ciencia/tripos-5084\n"
                "Latest user message: tripos (see "
                "https://noticias.ulp.edu.ar/ciencia/tripos-5084)"
            ),
        )
    ]
    assert (
        "Pre-explored context from the latest user message"
        in cast(
            "FakeStreamAgent",
            interviewer.agent,
        ).instructions
    )
    assert (
        "Tripos is a game similar to ta-te-ti"
        in cast(
            "FakeStreamAgent",
            interviewer.agent,
        ).instructions
    )
    tool_starts = [
        cast("dict[str, object]", event["data"])["tool_name"]
        for event in _read_events(log_path)
        if event["type"] == "tool_call_started"
    ]
    assert tool_starts == ["explore_context"]


def test_interviewer_coerces_raw_question_text(
    tmp_path: Path,
) -> None:
    """Raw question-format text becomes a structured question event."""
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([
            AgentRunResultEvent(
                result=FakeRunResult("Question (high): Who is this for?")
            )
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("idea")))

    assert [(event.kind, event.content) for event in events] == [
        ("tool_call", "ask_question"),
        (
            "question",
            InterviewQuestion(level="high", question="Who is this for?"),
        ),
    ]


def test_interviewer_coerces_streamed_raw_question_text(
    tmp_path: Path,
) -> None:
    """Streamed raw question-format text becomes a structured question."""
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([
            PartDeltaEvent(
                index=0,
                delta=TextPartDelta("Question (low): Which interface?"),
            ),
            AgentRunResultEvent(
                result=FakeRunResult("Question (low): Which interface?")
            ),
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("idea")))

    assert [(event.kind, event.content) for event in events] == [
        ("tool_call", "ask_question"),
        (
            "question",
            InterviewQuestion(level="low", question="Which interface?"),
        ),
    ]


def test_interviewer_coerces_raw_question_choices_and_default(
    tmp_path: Path,
) -> None:
    """Raw question text can include choices and a default."""
    raw_text = dedent("""\
        Question (low): Which interface?

        Choices: ; CLI - Terminal command; Web
        Default: CLI
        """)
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([AgentRunResultEvent(result=FakeRunResult(raw_text))]),
    )

    events = asyncio.run(_collect(interviewer.respond("idea")))

    assert events == [
        InterviewEvent(kind="tool_call", content="ask_question"),
        InterviewEvent(
            kind="question",
            content=InterviewQuestion(
                level="low",
                question="Which interface?",
                choices=(
                    QuestionChoice(
                        label="CLI",
                        description="Terminal command",
                    ),
                    QuestionChoice(label="Web"),
                ),
                default="CLI",
            ),
        ),
    ]


@pytest.mark.parametrize(
    "raw_text",
    [
        "Question (high): Who is this for?\nNot a labeled option",
        "Question (high): Who is this for?\nPriority: highest",
    ],
)
def test_interviewer_leaves_invalid_raw_question_text_visible(
    tmp_path: Path,
    raw_text: str,
) -> None:
    """Invalid raw question-like text remains normal assistant text."""
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([AgentRunResultEvent(result=FakeRunResult(raw_text))]),
    )

    events = asyncio.run(_collect(interviewer.respond("idea")))

    assert [(event.kind, event.content) for event in events] == [
        ("text", raw_text)
    ]


def test_interviewer_preserves_history_after_ask_tool_call(
    tmp_path: Path,
) -> None:
    """Ask tool turns keep user and assistant messages for the next reply."""
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    first_agent = FakeStreamAgent([
        FunctionToolCallEvent(
            ToolCallPart(
                "ask_question",
                args='{"level":"low","question":"Which interface?"}',
            )
        ),
        AgentRunResultEvent(result=FakeRunResult("ignored")),
    ])
    second_agent = FakeStreamAgent([
        AgentRunResultEvent(result=FakeRunResult("noted"))
    ])

    object.__setattr__(interviewer, "agent", first_agent)
    asyncio.run(_collect(interviewer.respond("idea")))
    object.__setattr__(interviewer, "agent", second_agent)
    asyncio.run(_collect(interviewer.respond("CLI")))

    assert len(second_agent.message_history) == 2
    request = cast("ModelRequest", second_agent.message_history[0])
    user_part = cast("UserPromptPart", request.parts[0])
    retained = cast("ModelResponse", second_agent.message_history[1])
    part = cast("TextPart", retained.parts[0])
    assert user_part.content == "idea"
    assert part.content == "Question (low): Which interface?"


def test_interviewer_yields_ask_tool_call_choices(
    tmp_path: Path,
) -> None:
    """Ask tool calls yield model-supplied choices."""
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    ask_args = (
        '{"level":"low","question":"Which interface should v1 use?",'
        '"choices":[{"label":"CLI","description":"Terminal command"}],'
        '"default":"CLI"}'
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([
            FunctionToolCallEvent(ToolCallPart("ask_question", args=ask_args)),
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("idea")))

    assert events[-1].content == InterviewQuestion(
        level="low",
        question="Which interface should v1 use?",
        choices=(QuestionChoice(label="CLI", description="Terminal command"),),
        default="CLI",
    )


@pytest.mark.parametrize(
    "args",
    [{"level": "middle"}, "{bad json", "[1]", None],
)
def test_interviewer_yields_invalid_ask_args_as_fallback(
    tmp_path: Path,
    args: object,
) -> None:
    """Invalid ask args fall back to a high-level clarification question."""
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    object.__setattr__(
        interviewer,
        "agent",
        FakeStreamAgent([
            FunctionToolCallEvent(ToolCallPart("ask_question", args=args)),
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("idea")))

    assert events[-1].content == InterviewQuestion(
        level="high",
        question="What should we clarify next?",
    )


def test_interviewer_tools_mutate_jri_state_and_finalize(
    tmp_path: Path,
) -> None:
    """Tool wrappers call core tools and log their results."""
    log_path = tmp_path / ".jri" / "logs" / "interview.jsonl"
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="just ralph it",
        logger=JsonlLogger(log_path),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))
    (tmp_path / ".jri" / "specs").mkdir(parents=True)
    (tmp_path / ".jri" / ".gitignore").write_text("logs/\n")
    _configure_repo(tmp_path)

    spec_result = asyncio.run(
        update_specs_tool(
            ctx,
            spec_name="product",
            content="# Product\n\n## Goal\n\nBuild a tiny CLI.\n",
        )
    )
    note_result = asyncio.run(
        update_scratchpad_tool(
            ctx,
            notes="# Notes\n",
        )
    )
    question = ask_question_tool(
        level="high",
        question="What should success look like?",
    )
    explore_result = asyncio.run(explore_context_tool(ctx, "Inspect README."))
    finalize_result = asyncio.run(
        finalize_specs_tool(
            ctx,
            readiness_summary="Ready.",
            spec_content=_complete_spec(),
            known_blockers=[],
        )
    )

    assert "product.md" in spec_result
    assert "scratchpad.md" in note_result
    assert (tmp_path / ".jri" / "specs" / "product.md").read_text(
        encoding="utf-8"
    ) == _complete_spec()
    assert (tmp_path / ".jri" / "scratchpad.md").read_text(
        encoding="utf-8"
    ) == "# Notes\n"
    assert question == "Question recorded for the next user turn."
    assert explore_result == "Summary:\n- inspected"
    assert "Ralph is coming soon to JRI" in finalize_result
    assert deps.finalized
    tool_starts = [
        cast("dict[str, object]", event["data"])["tool_name"]
        for event in _read_events(log_path)
        if event["type"] == "tool_call_started"
    ]
    assert tool_starts == [
        "update_specs",
        "update_scratchpad",
        "explore_context",
        "finalize_specs",
    ]


@pytest.mark.parametrize(
    "path_kind",
    [
        "absolute",
        "traversal",
    ],
)
def test_update_specs_tool_retries_invalid_spec_name_without_writing(
    tmp_path: Path,
    path_kind: str,
) -> None:
    """Invalid spec names become model retry guidance."""
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="idea",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))
    escaped_path = tmp_path / ".jri" / "escape.md"
    spec_name = "../escape.md"
    if path_kind == "absolute":
        escaped_path = tmp_path.parent / f"{tmp_path.name}-escape.md"
        spec_name = str(escaped_path)

    with pytest.raises(ModelRetry, match="Use a spec name"):
        asyncio.run(
            update_specs_tool(
                ctx,
                spec_name=spec_name,
                content="# Product\n\n## Goal\n\nBuild a tiny CLI.\n",
            )
        )

    assert not escaped_path.exists()
    assert not (tmp_path / ".jri" / "specs" / "escape.md").exists()
    failed = _latest_event(
        _read_events(tmp_path / "events.jsonl"), "tool_call_failed"
    )
    assert cast("dict[str, object]", failed["data"])["error_type"] == (
        "ModelRetry"
    )


@pytest.mark.parametrize(
    "content",
    [
        (
            "Use Rust for this:\n\n"
            "fn main() {\n"
            '    println!("Hello, world!");\n'
            "}\n"
        ),
        "The app should probably say hello when it runs.",
    ],
)
def test_update_specs_tool_retries_non_spec_markdown_without_writing(
    tmp_path: Path,
    content: str,
) -> None:
    """Raw prose or code is not accepted as persisted spec content."""
    product = tmp_path / ".jri" / "specs" / "product.md"
    product.parent.mkdir(parents=True)
    product.write_text("# Product\n\n## Goal\n\nExisting.\n", encoding="utf-8")
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="idea",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    with pytest.raises(ModelRetry, match="spec-shaped Markdown"):
        asyncio.run(
            update_specs_tool(
                ctx,
                spec_name="product",
                content=content,
            )
        )

    assert product.read_text(encoding="utf-8") == (
        "# Product\n\n## Goal\n\nExisting.\n"
    )


def test_update_scratchpad_tool_preserves_earlier_notes(
    tmp_path: Path,
) -> None:
    """Later note calls keep earlier confirmed interview facts."""
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="idea",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    asyncio.run(
        update_scratchpad_tool(
            ctx,
            notes="# Scratchpad\n\n## Confirmed\n\n- Goal: print hello.\n",
        )
    )
    asyncio.run(
        update_scratchpad_tool(
            ctx,
            notes="## Pending Questions\n\n- Who is the target user?\n",
        )
    )

    scratchpad = (tmp_path / ".jri" / "scratchpad.md").read_text(
        encoding="utf-8"
    )
    assert "Goal: print hello." in scratchpad
    assert "Who is the target user?" in scratchpad


def test_update_scratchpad_tool_does_not_duplicate_existing_notes(
    tmp_path: Path,
) -> None:
    """Repeated note calls leave the scratchpad stable."""
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="idea",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))
    notes = "# Scratchpad\n\n## Confirmed\n\n- Goal: print hello.\n"

    asyncio.run(update_scratchpad_tool(ctx, notes=notes))
    asyncio.run(update_scratchpad_tool(ctx, notes=notes))

    scratchpad = (tmp_path / ".jri" / "scratchpad.md").read_text(
        encoding="utf-8"
    )
    assert scratchpad.count("Goal: print hello.") == 1


def test_record_notes_tool_remains_backward_compatible(
    tmp_path: Path,
) -> None:
    """The old note wrapper still works for direct callers."""
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="idea",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    result = asyncio.run(record_notes_tool(ctx, notes="# Notes\n"))

    assert "scratchpad.md" in result
    assert (tmp_path / ".jri" / "scratchpad.md").read_text(
        encoding="utf-8"
    ) == "# Notes\n"


def test_interviewer_tools_patch_jri_state(tmp_path: Path) -> None:
    """Tool wrappers can apply focused Markdown patches."""
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="idea",
        logger=JsonlLogger(tmp_path / ".jri" / "logs" / "interview.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))
    specs = tmp_path / ".jri" / "specs"
    specs.mkdir(parents=True)
    (specs / "product.md").write_text("# Product\nold\n")
    (tmp_path / ".jri" / "scratchpad.md").write_text("# Scratchpad\nold\n")

    spec_result = asyncio.run(
        write_spec_tool(
            ctx,
            path="product",
            patch_text=(
                "*** Begin Patch\n"
                "*** Update File: product.md\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch"
            ),
        )
    )
    note_result = asyncio.run(
        write_note_tool(
            ctx,
            patch_text=(
                "*** Begin Patch\n"
                "*** Update File: scratchpad.md\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch"
            ),
        )
    )

    assert "M specs/product.md" in spec_result
    assert "M scratchpad.md" in note_result
    assert (specs / "product.md").read_text() == "# Product\nnew\n"
    assert (tmp_path / ".jri" / "scratchpad.md").read_text() == (
        "# Scratchpad\nnew\n"
    )


@pytest.mark.parametrize(
    "patch_text",
    ["# Scratchpad\n", "  ", "*** Begin Patch\n*** End Patch"],
)
def test_interviewer_write_tools_retry_invalid_patch_text(
    tmp_path: Path,
    patch_text: str,
) -> None:
    """Malformed patch_text becomes a model-visible retry prompt."""
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="idea",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    with pytest.raises(ModelRetry, match="complete patch envelope"):
        asyncio.run(write_note_tool(ctx, patch_text=patch_text))

    events = _read_events(tmp_path / "events.jsonl")
    started = _latest_event(events, "tool_call_started")
    failed = _latest_event(events, "tool_call_failed")
    assert cast("dict[str, object]", started["data"])["arguments"] == {
        "patch_text": patch_text
    }
    assert cast("dict[str, object]", failed["data"])["error_type"] == (
        "ModelRetry"
    )


def test_interviewer_write_tools_retry_failed_patch_context(
    tmp_path: Path,
) -> None:
    """Patch apply failures become model-visible retry prompts."""
    specs = tmp_path / ".jri" / "specs"
    specs.mkdir(parents=True)
    (specs / "product.md").write_text("# Product\nold\n", encoding="utf-8")
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="idea",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    with pytest.raises(ModelRetry, match="Failed to find context"):
        asyncio.run(
            write_spec_tool(
                ctx,
                path="product",
                patch_text=(
                    "*** Begin Patch\n"
                    "*** Update File: product.md\n"
                    "@@ -1,6 +1,6 @@\n"
                    "-old\n"
                    "+new\n"
                    "*** End Patch"
                ),
            )
        )

    failed = _latest_event(
        _read_events(tmp_path / "events.jsonl"), "tool_call_failed"
    )
    assert cast("dict[str, object]", failed["data"])["error_type"] == (
        "ModelRetry"
    )


def test_finalize_specs_tool_requires_spec_content(tmp_path: Path) -> None:
    """Finalization must persist concrete spec content."""
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="just ralph it",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    with pytest.raises(ModelRetry, match="non-empty"):
        asyncio.run(
            finalize_specs_tool(
                ctx,
                readiness_summary="Ready.",
                spec_content=" ",
                known_blockers=[],
            )
        )


def test_finalize_specs_tool_retries_invalid_spec_name(
    tmp_path: Path,
) -> None:
    """Finalization tells the model to use a spec name."""
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="just ralph it",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    with pytest.raises(ModelRetry, match="Use a spec name"):
        asyncio.run(
            finalize_specs_tool(
                ctx,
                readiness_summary="Ready.",
                spec_content=_complete_spec(),
                spec_name=str(tmp_path / "product.md"),
                known_blockers=[],
            )
        )

    events = _read_events(tmp_path / "events.jsonl")
    failed = _latest_event(events, "tool_call_failed")
    assert cast("dict[str, object]", failed["data"])["error_type"] == (
        "ModelRetry"
    )


def test_finalize_specs_tool_retries_without_trigger_phrase(
    tmp_path: Path,
) -> None:
    """The model must wait for an explicit trigger before finalization."""
    specs = tmp_path / ".jri" / "specs"
    specs.mkdir(parents=True)
    product = specs / "product.md"
    product.write_text("# Existing\n", encoding="utf-8")
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="not yet",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    with pytest.raises(ModelRetry, match="trigger phrase"):
        asyncio.run(
            finalize_specs_tool(
                ctx,
                readiness_summary="Ready.",
                spec_content=_complete_spec(),
                known_blockers=[],
            )
        )

    failed = _latest_event(
        _read_events(tmp_path / "events.jsonl"),
        "tool_call_failed",
    )
    assert cast("dict[str, object]", failed["data"])["error_type"] == (
        "ModelRetry"
    )
    assert product.read_text(encoding="utf-8") == "# Existing\n"


def test_finalize_specs_tool_reports_known_blockers_without_writing_spec(
    tmp_path: Path,
) -> None:
    """Known blockers prevent spec persistence and finalization."""
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="just ralph it",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    result = asyncio.run(
        finalize_specs_tool(
            ctx,
            readiness_summary="Not ready.",
            spec_content="# Product\n",
            known_blockers=["Missing target user"],
        )
    )

    assert result == "Cannot finalize specs yet:\n- Missing target user"
    assert not (tmp_path / ".jri" / "specs" / "product.md").exists()
    assert not deps.finalized


@pytest.mark.parametrize("spec_content", ["", " \n\t"])
def test_finalize_specs_tool_reports_known_blockers_before_blank_content_retry(
    tmp_path: Path,
    spec_content: str,
) -> None:
    """Known blockers are returned before retrying missing final content."""
    product = tmp_path / ".jri" / "specs" / "product.md"
    product.parent.mkdir(parents=True)
    product.write_text("# Existing\n", encoding="utf-8")
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="just ralph it",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    result = asyncio.run(
        finalize_specs_tool(
            ctx,
            readiness_summary="Not ready.",
            spec_content=spec_content,
            known_blockers=["Missing target user"],
        )
    )

    assert result == "Cannot finalize specs yet:\n- Missing target user"
    assert product.read_text(encoding="utf-8") == "# Existing\n"
    assert not deps.finalized


def test_finalize_specs_tool_reports_incomplete_spec_without_writing(
    tmp_path: Path,
) -> None:
    """Missing readiness facts prevent final spec replacement."""
    product = tmp_path / ".jri" / "specs" / "product.md"
    product.parent.mkdir(parents=True)
    product.write_text("# Existing\n", encoding="utf-8")
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="just ralph it",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    result = asyncio.run(
        finalize_specs_tool(
            ctx,
            readiness_summary="Ready.",
            spec_content="# Product\n\n## Goal\n\nBuild a tiny CLI.\n",
            known_blockers=[],
        )
    )

    assert result.startswith("Missing MVP readiness facts:")
    assert product.read_text(encoding="utf-8") == "# Existing\n"
    assert not deps.finalized


def test_finalize_specs_tool_retries_core_finalization_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core finalization errors are converted into model retry guidance."""
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="just ralph it",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    async def fail_finalize(**kwargs: object) -> object:
        _ = kwargs
        msg = "git failed"
        raise JustRalphItError(msg)

    monkeypatch.setattr(
        "jri.core.tools.interviewer.finalize_jri",
        fail_finalize,
    )

    with pytest.raises(ModelRetry, match="git failed"):
        asyncio.run(
            finalize_specs_tool(
                ctx,
                readiness_summary="Ready.",
                spec_content=_complete_spec(),
                known_blockers=[],
            )
        )


def test_logged_tool_records_failure(tmp_path: Path) -> None:
    """Tool wrapper failures are logged before propagating."""
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="idea",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    with contextlib.suppress(Exception):
        asyncio.run(
            write_spec_tool(
                ctx,
                path="../escape.md",
                patch_text=(
                    "*** Begin Patch\n"
                    "*** Add File: ../escape.md\n"
                    "+# Escape\n"
                    "*** End Patch"
                ),
            )
        )

    log = (tmp_path / "events.jsonl").read_text()
    assert "tool_call_failed" in log


def _read_events(path: Path) -> list[dict[str, object]]:
    return [
        cast("dict[str, object]", json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _latest_event(
    events: list[dict[str, object]],
    event_type: str,
) -> dict[str, object]:
    return next(
        event for event in reversed(events) if event["type"] == event_type
    )


def _event_by_call_id(
    events: list[dict[str, object]],
    *,
    event_type: str,
    tool_call_id: str,
) -> dict[str, object]:
    return next(
        event
        for event in events
        if event["type"] == event_type
        and cast("dict[str, object]", event["data"])["tool_call_id"]
        == tool_call_id
    )


async def _collect(iterator: AsyncIterator[object]) -> list[object]:
    return [event async for event in iterator]


async def _receive_first_event_before_stream_completion(
    tmp_path: Path,
) -> tuple[bool, object]:
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    fake_agent = BlockingAfterFirstTextStreamAgent()
    object.__setattr__(interviewer, "agent", fake_agent)
    iterator: AsyncIterator[object] = interviewer.respond("idea")
    first_event_task: asyncio.Task[object] = asyncio.create_task(
        _receive_next_item(iterator)
    )

    await asyncio.wait_for(
        fake_agent.first_provider_event_seen.wait(),
        timeout=1,
    )
    done: set[asyncio.Task[object]]
    pending: set[asyncio.Task[object]]
    done, pending = await asyncio.wait({first_event_task}, timeout=0.1)
    _ = pending
    first_event_was_progressive = first_event_task in done
    stream_completed_before_first = fake_agent.stream_completed.is_set()

    fake_agent.allow_completion.set()
    first_event = await asyncio.wait_for(first_event_task, timeout=1)
    _ = [event async for event in iterator]

    assert not stream_completed_before_first
    assert fake_agent.stream_completed.is_set()
    return first_event_was_progressive, first_event


async def _receive_next_item(iterator: AsyncIterator[object]) -> object:
    return await anext(iterator)


async def _collect_delayed_ask_events(
    tmp_path: Path,
) -> tuple[bool, list[object]]:
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    fake_agent = BlockingBeforeAskStreamAgent()
    object.__setattr__(interviewer, "agent", fake_agent)
    iterator: AsyncIterator[object] = interviewer.respond("idea")
    first_event_task: asyncio.Task[object] = asyncio.create_task(
        _receive_next_item(iterator)
    )

    await asyncio.wait_for(
        fake_agent.first_provider_event_seen.wait(),
        timeout=1,
    )
    done, pending = await asyncio.wait({first_event_task}, timeout=0.1)
    _ = pending
    first_event_was_waiting = first_event_task not in done

    fake_agent.allow_ask.set()
    first_event = await asyncio.wait_for(first_event_task, timeout=1)
    remaining_events = [event async for event in iterator]
    return first_event_was_waiting, [first_event, *remaining_events]


async def _close_iterator_after_first_event(
    tmp_path: Path,
) -> tuple[object, bool]:
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    fake_agent = BlockingAfterToolCallStreamAgent()
    object.__setattr__(interviewer, "agent", fake_agent)
    iterator: AsyncIterator[object] = interviewer.respond("idea")

    first_event = await asyncio.wait_for(anext(iterator), timeout=1)
    await iterator.aclose()
    await asyncio.wait_for(fake_agent.stream_closed.wait(), timeout=1)
    return first_event, fake_agent.stream_closed.is_set()


async def _collect_delayed_tool_result_after_ask(
    tmp_path: Path,
    *,
    fake_agent: DelayedToolResultAfterAskStreamAgent | None = None,
) -> tuple[bool, list[InterviewEvent]]:
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    if fake_agent is None:
        fake_agent = DelayedToolResultAfterAskStreamAgent(tmp_path)
    object.__setattr__(interviewer, "agent", fake_agent)
    iterator: AsyncIterator[InterviewEvent] = interviewer.respond("idea")

    first_event = await asyncio.wait_for(anext(iterator), timeout=1)
    next_event_task: asyncio.Task[object] = asyncio.create_task(
        _receive_next_item(iterator)
    )
    done, pending = await asyncio.wait({next_event_task}, timeout=0.1)
    _ = pending
    question_was_visible_early = next_event_task in done
    if question_was_visible_early:
        early_event = cast("InterviewEvent", await next_event_task)
        remaining_events = [event async for event in iterator]
        return True, [first_event, early_event, *remaining_events]

    fake_agent.allow_tool_result.set()
    next_event = cast(
        "InterviewEvent",
        await asyncio.wait_for(next_event_task, timeout=1),
    )
    remaining_events = [event async for event in iterator]
    return False, [first_event, next_event, *remaining_events]


def _configure_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "jri@example.com"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "JRI Tests"],
        cwd=path,
        check=True,
    )


def _has_commit(path: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=path,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _complete_spec() -> str:
    return (
        "# Product\n\n"
        "## Goal\n\n"
        "Build a tiny CLI that prints hello.\n\n"
        "## Target User\n\n"
        "Programmers trying JRI locally.\n\n"
        "## Workflows\n\n"
        "The user runs the CLI command once.\n\n"
        "## Inputs\n\n"
        "No user input is required.\n\n"
        "## Outputs\n\n"
        "The CLI prints hello to stdout.\n\n"
        "## Persistence\n\n"
        "No data is saved.\n\n"
        "## Integrations\n\n"
        "No external integrations are used.\n\n"
        "## Errors\n\n"
        "If the command fails, it exits non-zero.\n\n"
        "## Edge Cases\n\n"
        "Repeated runs print the same output.\n\n"
        "## Non-goals\n\n"
        "No interactive prompt in v1.\n\n"
        "## Success Criteria\n\n"
        "Running the command prints hello exactly once.\n"
    )
