"""Terminal REPL for interviewer sessions."""

import asyncio
import contextlib
import sys
from typing import TextIO, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.styles import Style
from pydantic_ai import UnexpectedModelBehavior

from jri.cli.rendering import render_prompt, render_question, render_tool_call
from jri.core.interview import InterviewQuestion, InterviewSession
from jri.core.logging import JsonlLogger
from jri.core.project import ProjectState
from jri.core.triggers import is_trigger_message

FINALIZATION_STARTED_MESSAGE = "Finalizing specs..."
MODEL_ERROR_RECOVERY_MESSAGE = "I hit a model/tool issue. Please try again."
SHIFT_ENTER_SEQUENCES = ("\x1b[13;2u", "\x1b[27;2;13~")


def run_repl(
    *,
    state: ProjectState,
    interviewer: InterviewSession,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
) -> int:
    """Run the interviewer REPL."""
    return asyncio.run(
        _run_repl(
            state=state,
            interviewer=interviewer,
            input_stream=input_stream or sys.stdin,
            output_stream=output_stream or sys.stdout,
            error_stream=error_stream or sys.stderr,
        )
    )


async def _run_repl(
    *,
    state: ProjectState,
    interviewer: InterviewSession,
    input_stream: TextIO,
    output_stream: TextIO,
    error_stream: TextIO,
) -> int:
    logger = JsonlLogger(state.jri_dir / "logs" / "interview.jsonl")
    logger.write("session_started", {"project_root": str(state.root)})
    reader = _InputReader(
        input_stream=input_stream, output_stream=output_stream
    )

    while True:
        try:
            user_message = await reader.read()
        except asyncio.CancelledError:
            error_stream.write("Cancelled.\n")
            logger.write("session_finished", {"reason": "keyboard_interrupt"})
            return 130
        except EOFError:
            logger.write("session_finished", {"reason": "eof"})
            return 0
        except KeyboardInterrupt:
            error_stream.write("Cancelled.\n")
            logger.write("session_finished", {"reason": "keyboard_interrupt"})
            return 130

        if not user_message.strip():
            continue

        logger.write("user_message", {"message": user_message})
        try:
            await _run_turn(
                interviewer=interviewer,
                logger=logger,
                output_stream=output_stream,
                user_message=user_message,
            )
        except UnexpectedModelBehavior as exc:
            logger.write(
                "model_error",
                {"message": str(exc), "error_type": type(exc).__name__},
            )
            _write_assistant_text(output_stream, MODEL_ERROR_RECOVERY_MESSAGE)
            logger.write(
                "assistant_message",
                {"message": MODEL_ERROR_RECOVERY_MESSAGE},
            )
            output_stream.flush()
            continue
        except Exception as exc:  # noqa: BLE001
            logger.write("error", {"message": str(exc)})
            error_stream.write(f"{exc}\n")
            error_stream.flush()
            continue

        if interviewer.should_exit:
            logger.write("session_finished", {"reason": "just_ralph_it"})
            return 0


class _InputReader:
    def __init__(self, *, input_stream: TextIO, output_stream: TextIO) -> None:
        self.input_stream: TextIO = input_stream
        self.output_stream: TextIO = output_stream
        self.session: PromptSession[str] | None = None

    async def read(self) -> str:
        if self.input_stream.isatty():
            if self.session is None:
                # PromptSession inspects terminal state on construction.
                # Create it only for real TTY sessions.
                self.session = _create_prompt_session()
            return await self.session.prompt_async(
                FormattedText([("class:prompt", "jri> ")]),
                style=Style.from_dict({"prompt": "ansicyan"}),
            )

        self.output_stream.write(render_prompt())
        self.output_stream.flush()
        line = self.input_stream.readline()
        if not line:
            raise EOFError
        return line.rstrip("\n")


def _create_prompt_session() -> PromptSession[str]:
    _install_terminal_sequences()
    bindings = KeyBindings()

    @bindings.add("c-j")
    def _insert_newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    def _move_left(event: KeyPressEvent) -> None:
        event.current_buffer.cursor_position += (
            event.current_buffer.document.get_cursor_left_position(
                count=event.arg
            )
        )

    bindings.add("s-left")(_move_left)
    bindings.add("c-s-left")(_move_left)

    def _move_right(event: KeyPressEvent) -> None:
        event.current_buffer.cursor_position += (
            event.current_buffer.document.get_cursor_right_position(
                count=event.arg
            )
        )

    bindings.add("s-right")(_move_right)
    bindings.add("c-s-right")(_move_right)

    def _move_up(event: KeyPressEvent) -> None:
        event.current_buffer.auto_up(count=event.arg)

    bindings.add("s-up")(_move_up)
    bindings.add("c-s-up")(_move_up)

    def _move_down(event: KeyPressEvent) -> None:
        event.current_buffer.auto_down(count=event.arg)

    bindings.add("s-down")(_move_down)
    bindings.add("c-s-down")(_move_down)

    with contextlib.suppress(ValueError):
        bindings.add("s-enter")(_insert_newline)

    return PromptSession(multiline=False, key_bindings=bindings)


def _install_terminal_sequences() -> None:
    for sequence in SHIFT_ENTER_SEQUENCES:
        ANSI_SEQUENCES[sequence] = Keys.ControlJ


def _write_assistant_text(output_stream: TextIO, content: str) -> None:
    output_stream.write(content)
    if not content.endswith("\n"):
        output_stream.write("\n")


async def _run_turn(
    *,
    interviewer: InterviewSession,
    logger: JsonlLogger,
    output_stream: TextIO,
    user_message: str,
) -> None:
    assistant_parts: list[str] = []
    streamed_text = False
    if is_trigger_message(user_message):
        _write_assistant_text(output_stream, FINALIZATION_STARTED_MESSAGE)
        output_stream.flush()

    async for event in interviewer.respond(user_message):
        if event.kind == "tool_call":
            tool_name = cast("str", event.content)
            output_stream.write(render_tool_call(tool_name))
            logger.write("tool_call_rendered", {"tool_name": tool_name})
        elif event.kind == "question":
            rendered = render_question(
                cast("InterviewQuestion", event.content)
            )
            _write_assistant_text(output_stream, rendered)
            assistant_parts.append(rendered)
        elif event.kind == "text":
            content = cast("str", event.content)
            _write_assistant_text(output_stream, content)
            assistant_parts.append(content)
        else:
            content = cast("str", event.content)
            output_stream.write(content)
            assistant_parts.append(content)
            streamed_text = True
        output_stream.flush()

    assistant_message = "".join(assistant_parts)
    if assistant_message:
        if streamed_text and not assistant_message.endswith("\n"):
            output_stream.write("\n")
        logger.write("assistant_message", {"message": assistant_message})
        output_stream.flush()
