"""Agent adapter test doubles."""

from collections.abc import AsyncIterator, Iterable
from typing import Self, cast


class FakeRunContext:
    """Small object with the RunContext field tools use."""

    def __init__(self, deps: object) -> None:
        self.deps: object = deps


class FakeRunResult:
    """Agent run result stand-in."""

    def __init__(
        self,
        output: str,
        *,
        run_id: str = "fake-run",
        conversation_id: str = "fake-conversation",
        usage: object | None = None,
        response: object | None = None,
    ) -> None:
        self.output: str = output
        self.run_id: str = run_id
        self.conversation_id: str = conversation_id
        self.usage: object | None = usage
        self.response: object | None = response
        self.messages: list[object] = []

    def all_messages(self) -> list[object]:
        """Return fake model history."""
        return self.messages


class FakeStreamAgent:
    """Agent stand-in returning an async event stream."""

    def __init__(self, events: list[object]) -> None:
        self.events: list[object] = events
        self.instructions: str = ""
        self.message_history: list[object] = []

    def run_stream_events(
        self, *args: object, **kwargs: object
    ) -> "FakeStream":
        """Return a fake stream and record run instructions."""
        _ = args
        self.instructions = str(kwargs["instructions"])
        self.message_history = list(
            cast("Iterable[object]", kwargs["message_history"])
        )
        return FakeStream(self.events)


class FakeStream:
    """Async context manager over fake agent events."""

    def __init__(self, events: list[object]) -> None:
        self.events: list[object] = events

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
        return _iterate(self.events)


async def _iterate(events: list[object]) -> AsyncIterator[object]:
    for event in events:
        yield event


class FakeRunAgent:
    """Agent stand-in for non-streaming runs."""

    def __init__(self, output: str) -> None:
        self.output: str = output
        self.requests: list[str] = []

    async def run(self, request: str, *, deps: object) -> FakeRunResult:
        """Record requests and return a fake result."""
        _ = deps
        self.requests.append(request)
        return FakeRunResult(self.output)
