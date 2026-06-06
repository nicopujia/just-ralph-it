# pyright: reportImplicitOverride=false, reportPrivateUsage=false, reportUnannotatedClassAttribute=false, reportUnreachable=false
"""Tests for REPL control flow."""

import asyncio
from io import StringIO
from pathlib import Path

import pytest

from jri.cli import repl
from jri.core.project import ProjectState
from tests.doubles.repl import (
    DeltaInterviewer,
    EchoInterviewer,
    FailingInterviewer,
    FakeInput,
    FakePromptSession,
    InterruptingInput,
    NoopInterviewer,
    QuestionInterviewer,
)


def test_repl_returns_130_on_keyboard_interrupt(tmp_path: Path) -> None:
    """Keyboard interrupt while reading input cancels the session."""
    err = StringIO()

    code = repl.run_repl(
        state=_state(tmp_path),
        interviewer=NoopInterviewer(),
        input_stream=InterruptingInput(),
        output_stream=StringIO(),
        error_stream=err,
    )

    assert code == 130
    assert "Cancelled." in err.getvalue()


def test_repl_recovers_from_interviewer_error(tmp_path: Path) -> None:
    """Recoverable turn errors keep the REPL open until EOF."""
    err = StringIO()

    code = repl.run_repl(
        state=_state(tmp_path),
        interviewer=FailingInterviewer(),
        input_stream=FakeInput("idea\n"),
        output_stream=StringIO(),
        error_stream=err,
    )

    assert code == 0
    assert "agent failed" in err.getvalue()


def test_repl_skips_blank_input_then_handles_message(tmp_path: Path) -> None:
    """Blank messages are ignored before the next real message."""
    out = StringIO()
    interviewer = EchoInterviewer()

    code = repl.run_repl(
        state=_state(tmp_path),
        interviewer=interviewer,
        input_stream=FakeInput("\nhello\n"),
        output_stream=out,
        error_stream=StringIO(),
    )

    assert code == 0
    assert interviewer.messages == ["hello"]
    assert "echo: hello" in out.getvalue()


def test_repl_accepts_turn_without_assistant_output(tmp_path: Path) -> None:
    """A silent interviewer turn still returns to the input loop."""
    out = StringIO()

    code = repl.run_repl(
        state=_state(tmp_path),
        interviewer=NoopInterviewer(),
        input_stream=FakeInput("hello\n"),
        output_stream=out,
        error_stream=StringIO(),
    )

    assert code == 0
    assert out.getvalue().count("jri>") == 2


def test_repl_streams_text_deltas_without_extra_newlines(
    tmp_path: Path,
) -> None:
    """Streaming chunks are rendered as one assistant message."""
    out = StringIO()

    code = repl.run_repl(
        state=_state(tmp_path),
        interviewer=DeltaInterviewer(),
        input_stream=FakeInput("hello\n"),
        output_stream=out,
        error_stream=StringIO(),
    )

    assert code == 0
    assert "hello world\n" in out.getvalue()


def test_repl_does_not_add_extra_newline_to_complete_text(
    tmp_path: Path,
) -> None:
    """Assistant text that already ends in a newline is not padded."""
    out = StringIO()

    code = repl.run_repl(
        state=_state(tmp_path),
        interviewer=EchoInterviewer(response_suffix="\n"),
        input_stream=FakeInput("hello\n"),
        output_stream=out,
        error_stream=StringIO(),
    )

    assert code == 0
    assert "echo: hello\n\n" not in out.getvalue()


def test_repl_renders_structured_questions(tmp_path: Path) -> None:
    """Structured question events render as assistant text."""
    out = StringIO()

    code = repl.run_repl(
        state=_state(tmp_path),
        interviewer=QuestionInterviewer(),
        input_stream=FakeInput("hello\n"),
        output_stream=out,
        error_stream=StringIO(),
    )

    assert code == 0
    assert "High-level question:\nWho is this for?\n" in out.getvalue()


def test_input_reader_uses_prompt_toolkit_for_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTY input is delegated to prompt_toolkit."""
    fake_session = FakePromptSession()
    monkeypatch.setattr(
        "jri.cli.repl._create_prompt_session",
        lambda: fake_session,
    )
    reader = repl._InputReader(  # noqa: SLF001
        input_stream=FakeInput("", tty=True),
        output_stream=StringIO(),
    )

    assert asyncio.run(reader.read()) == "typed"
    assert asyncio.run(reader.read()) == "typed"
    assert fake_session.prompted


def test_create_prompt_session_returns_multiline_session() -> None:
    """Prompt session construction wires key bindings successfully."""
    session = repl._create_prompt_session()  # noqa: SLF001

    assert session.multiline is False


def _state(tmp_path: Path) -> ProjectState:
    (tmp_path / ".jri" / "logs").mkdir(parents=True)
    return ProjectState(root=tmp_path)
