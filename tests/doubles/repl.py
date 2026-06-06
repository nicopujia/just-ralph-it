# pyright: reportUnreachable=false
"""REPL test doubles."""

from collections.abc import AsyncIterator
from io import StringIO
from typing import override

from jri.core.interview import InterviewEvent, InterviewQuestion


class FakeInput(StringIO):
    """StringIO with controllable TTY status."""

    def __init__(self, value: str, *, tty: bool = False) -> None:
        super().__init__(value)
        self.tty: bool = tty

    @override
    def isatty(self) -> bool:
        """Return configured TTY status."""
        return self.tty


class InterruptingInput(StringIO):
    """Input stream that raises KeyboardInterrupt."""

    @override
    def isatty(self) -> bool:
        """Return non-TTY status."""
        return False

    @override
    def readline(self, size: int = -1) -> str:
        """Raise KeyboardInterrupt while reading."""
        _ = size
        raise KeyboardInterrupt


class FakePromptSession:
    """PromptSession stand-in."""

    def __init__(self) -> None:
        self.prompted: bool = False

    async def prompt_async(self, *args: object, **kwargs: object) -> str:
        """Return fake typed input."""
        _ = (args, kwargs)
        self.prompted = True
        return "typed"


class NoopInterviewer:
    """Interviewer that never responds."""

    @property
    def should_exit(self) -> bool:
        """Return false."""
        return False

    async def respond(
        self,
        user_message: str,
    ) -> AsyncIterator[InterviewEvent]:
        """Yield no events."""
        _ = user_message
        if False:
            yield InterviewEvent(kind="text", content="")


class FailingInterviewer(NoopInterviewer):
    """Interviewer that raises once."""

    @override
    async def respond(
        self,
        user_message: str,
    ) -> AsyncIterator[InterviewEvent]:
        """Raise a recoverable error."""
        _ = user_message
        msg = "agent failed"
        raise RuntimeError(msg)
        if False:
            yield InterviewEvent(kind="text", content="")


class EchoInterviewer(NoopInterviewer):
    """Interviewer that echoes input."""

    def __init__(self, *, response_suffix: str = "") -> None:
        self.messages: list[str] = []
        self.response_suffix: str = response_suffix

    @override
    async def respond(
        self,
        user_message: str,
    ) -> AsyncIterator[InterviewEvent]:
        """Yield one echo message."""
        self.messages.append(user_message)
        yield InterviewEvent(
            kind="text",
            content=f"echo: {user_message}{self.response_suffix}",
        )


class DeltaInterviewer(NoopInterviewer):
    """Interviewer that streams text chunks."""

    @override
    async def respond(
        self,
        user_message: str,
    ) -> AsyncIterator[InterviewEvent]:
        """Yield two text deltas."""
        _ = user_message
        yield InterviewEvent(kind="text_delta", content="hello")
        yield InterviewEvent(kind="text_delta", content=" world")


class QuestionInterviewer(NoopInterviewer):
    """Interviewer that asks one structured question."""

    @override
    async def respond(
        self,
        user_message: str,
    ) -> AsyncIterator[InterviewEvent]:
        """Yield a structured question event."""
        _ = user_message
        yield InterviewEvent(
            kind="question",
            content=InterviewQuestion(
                level="high",
                question="Who is this for?",
            ),
        )
