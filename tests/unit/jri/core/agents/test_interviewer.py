# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportInvalidCast=false, reportUnknownMemberType=false
"""Tests for the live interviewer adapter."""

import asyncio
import contextlib
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from pydantic_ai import (
    AgentRunResultEvent,
    FunctionToolCallEvent,
    PartDeltaEvent,
    RunContext,
    TextPartDelta,
)
from pydantic_ai.messages import ToolCallPart

from jri.core.agents.interviewer import (
    Interviewer,
    InterviewerDeps,
    ask_question_tool,
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
    FakeStreamAgent,
)
from tests.doubles.explorers import RecordingExplorer


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
    interviewer = Interviewer(
        project_root=tmp_path,
        logger=JsonlLogger(tmp_path / "events.jsonl"),
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


def test_finalize_tool_requires_spec_content(tmp_path: Path) -> None:
    """Finalization must persist concrete spec content."""
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="just ralph it",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    with pytest.raises(JustRalphItError, match="non-empty"):
        asyncio.run(
            finalize_tool(
                ctx,
                readiness_summary="Ready.",
                spec_content=" ",
                known_blockers=[],
            )
        )


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

    with pytest.raises(JustRalphItError, match="Missing target user"):
        asyncio.run(
            finalize_tool(
                ctx,
                readiness_summary="Not ready.",
                spec_content="# Product\n",
                known_blockers=["Missing target user"],
            )
        )

    assert not (tmp_path / ".jri" / "specs" / "product.md").exists()


def test_logged_tool_records_failure(tmp_path: Path) -> None:
    """Tool wrapper failures are logged before propagating."""
    deps = InterviewerDeps(
        project_root=tmp_path,
        latest_user_message="idea",
        logger=JsonlLogger(tmp_path / "events.jsonl"),
        explorer=RecordingExplorer(),
    )
    ctx = cast("RunContext[InterviewerDeps]", FakeRunContext(deps))

    with contextlib.suppress(ValueError):
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
