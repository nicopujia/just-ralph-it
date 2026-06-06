# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportInvalidCast=false, reportUnknownMemberType=false
"""Tests for the live interviewer adapter."""

import asyncio
import contextlib
import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast, override

import pytest
from pydantic_ai import (
    AgentRunResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRetry,
    PartDeltaEvent,
    RunContext,
    TextPartDelta,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.usage import RequestUsage, RunUsage

from jri.core.agents.interviewer import (
    Interviewer,
    InterviewerDeps,
    ask_question_tool,
    build_interviewer_tools,
    explore_tool,
    finalize_tool,
    write_note_tool,
    write_spec_tool,
)
from jri.core.agents.models import AgentModelConfig
from jri.core.interview import InterviewQuestion, QuestionChoice
from jri.core.logging import JsonlLogger
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
        FunctionToolCallEvent(ToolCallPart("spec")),
        PartDeltaEvent(index=0, delta=TextPartDelta("hello")),
        AgentRunResultEvent(result=FakeRunResult("ignored")),
    ])
    object.__setattr__(interviewer, "agent", fake_agent)

    events = asyncio.run(_collect(interviewer.respond("idea")))

    assert not interviewer.should_exit
    assert [(event.kind, event.content) for event in events] == [
        ("tool_call", "spec"),
        ("text_delta", "hello"),
    ]
    assert fake_agent.instructions


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
                    "spec",
                    args={"path": "product", "patch_text": "not a patch"},
                    tool_call_id="call-1",
                )
            ),
            FunctionToolCallEvent(
                ToolCallPart(
                    "note",
                    args={"items": ["value", ("tuple", StableRepr())]},
                    tool_call_id="call-2",
                )
            ),
            FunctionToolResultEvent(
                ToolReturnPart(
                    tool_name="spec",
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
        "tool_name": "spec",
        "tool_call_id": "call-1",
        "args": {"path": "product", "patch_text": "not a patch"},
        "args_json": '{"path":"product","patch_text":"not a patch"}',
    }
    assert tool_result["data"] == {
        "tool_name": "spec",
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


def test_interviewer_registers_strict_patch_tools(tmp_path: Path) -> None:
    """Provider schemas for mutation tools require strict arguments."""
    _ = tmp_path
    tools = {tool.name: tool for tool in build_interviewer_tools()}

    assert tools["spec"].strict is True
    assert tools["spec"].max_retries == 3
    spec_schema = tools["spec"].function_schema.json_schema
    note_schema = tools["note"].function_schema.json_schema
    assert spec_schema["required"] == [
        "path",
        "patch_text",
    ]
    path_description = cast(
        "str",
        spec_schema["properties"]["path"]["description"],
    )
    assert "relative" in path_description.lower()
    assert (
        "every body line must start with +"
        in (spec_schema["properties"]["patch_text"]["description"])
    )
    assert tools["note"].strict is True
    assert tools["note"].max_retries == 3
    assert note_schema["required"] == ["patch_text"]
    assert tools["just_ralph_it"].strict is True
    assert tools["just_ralph_it"].max_retries == 3


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
        "# Product\n\nA tiny CLI prints hello to stdout.\n",
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
        ("tool_call", "just_ralph_it"),
        (
            "text",
            (
                "Specs finalized for Ralph handoff and committed. Readiness: "
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
                    "ask",
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
            FunctionToolCallEvent(ToolCallPart("ask", args=ask_args)),
            AgentRunResultEvent(result=FakeRunResult("ignored")),
        ]),
    )

    events = asyncio.run(_collect(interviewer.respond("idea")))

    assert [(event.kind, event.content) for event in events] == [
        ("tool_call", "ask"),
        (
            "question",
            InterviewQuestion(level="high", question="Who is this for?"),
        ),
    ]
    assert _latest_event(_read_events(log_path), "model_text_delta")[
        "data"
    ] == {"content": "Preamble."}


def test_interviewer_preserves_history_after_ask_tool_call(
    tmp_path: Path,
) -> None:
    """Ask tool turns keep their model messages for the next reply."""
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        model_config=AgentModelConfig("test", "test"),
    )
    first_agent = FakeStreamAgent([
        FunctionToolCallEvent(
            ToolCallPart(
                "ask",
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

    assert len(second_agent.message_history) == 1
    retained = cast("ModelResponse", second_agent.message_history[0])
    part = cast("TextPart", retained.parts[0])
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
            FunctionToolCallEvent(ToolCallPart("ask", args=ask_args)),
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
            FunctionToolCallEvent(ToolCallPart("ask", args=args)),
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
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="just ralph it",
        logger=JsonlLogger(tmp_path / ".jri" / "logs" / "interview.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))
    (tmp_path / ".jri" / "specs").mkdir(parents=True)
    (tmp_path / ".jri" / ".gitignore").write_text("logs/\n")
    _configure_repo(tmp_path)

    spec_result = asyncio.run(
        write_spec_tool(
            ctx,
            path="product",
            patch_text=(
                "*** Begin Patch\n"
                "*** Add File: product.md\n"
                "+# Product\n"
                "*** End Patch"
            ),
        )
    )
    note_result = asyncio.run(
        write_note_tool(
            ctx,
            patch_text=(
                "*** Begin Patch\n"
                "*** Add File: scratchpad.md\n"
                "+# Scratchpad\n"
                "*** End Patch"
            ),
        )
    )
    question = ask_question_tool(
        level="high",
        question="What should success look like?",
    )
    explore_result = asyncio.run(explore_tool(ctx, "Inspect README."))
    finalize_result = asyncio.run(
        finalize_tool(
            ctx,
            readiness_summary="Ready.",
            spec_content="# Product\n",
            known_blockers=[],
        )
    )

    assert "product.md" in spec_result
    assert "scratchpad.md" in note_result
    assert question == "Question recorded for the next user turn."
    assert explore_result == "Summary:\n- inspected"
    assert "Ralph handoff" in finalize_result
    assert deps.finalized


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


def test_finalize_tool_requires_spec_content(tmp_path: Path) -> None:
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
            finalize_tool(
                ctx,
                readiness_summary="Ready.",
                spec_content=" ",
                known_blockers=[],
            )
        )


def test_finalize_tool_retries_absolute_spec_path(tmp_path: Path) -> None:
    """Finalization tells the model to use relative spec paths."""
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="just ralph it",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    with pytest.raises(ModelRetry, match="Never pass an absolute path"):
        asyncio.run(
            finalize_tool(
                ctx,
                readiness_summary="Ready.",
                spec_content="# Product\n",
                spec_path=str(tmp_path / "product.md"),
                known_blockers=[],
            )
        )

    events = _read_events(tmp_path / "events.jsonl")
    failed = _latest_event(events, "tool_call_failed")
    assert cast("dict[str, object]", failed["data"])["error_type"] == (
        "ModelRetry"
    )


def test_finalize_tool_retries_without_trigger_phrase(tmp_path: Path) -> None:
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
            finalize_tool(
                ctx,
                readiness_summary="Ready.",
                spec_content="# Product\n",
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


def test_finalize_tool_rejects_known_blockers_without_writing_spec(
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

    with pytest.raises(ModelRetry, match="Missing target user"):
        asyncio.run(
            finalize_tool(
                ctx,
                readiness_summary="Not ready.",
                spec_content="# Product\n",
                known_blockers=["Missing target user"],
            )
        )

    assert not (tmp_path / ".jri" / "specs" / "product.md").exists()


def test_finalize_tool_retries_core_finalization_errors(
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
        "jri.core.agents.interviewer.finalize_jri",
        fail_finalize,
    )

    with pytest.raises(ModelRetry, match="git failed"):
        asyncio.run(
            finalize_tool(
                ctx,
                readiness_summary="Ready.",
                spec_content="# Product\n",
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
