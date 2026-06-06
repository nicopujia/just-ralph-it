"""Terminal REPL for interviewer sessions."""

import asyncio
import contextlib
import sys
from typing import TextIO, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.styles import Style

from jri.cli.rendering import render_prompt, render_question, render_tool_call
from jri.core.interview import InterviewQuestion, InterviewSession
from jri.core.logging import JsonlLogger
from jri.core.project import ProjectState


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
    bindings = KeyBindings()

    @bindings.add("c-j")
    def _insert_newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")  # pragma: no cover

    with contextlib.suppress(ValueError):
        bindings.add("s-enter")(_insert_newline)

    return PromptSession(multiline=False, key_bindings=bindings)


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
    async for event in interviewer.respond(user_message):
        if event.kind == "tool_call":
            tool_name = cast("str", event.content)
            output_stream.write(render_tool_call(tool_name))
            logger.write("tool_call_started", {"tool_name": tool_name})
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
