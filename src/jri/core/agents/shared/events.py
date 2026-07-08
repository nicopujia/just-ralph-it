from dataclasses import dataclass


@dataclass(frozen=True)
class TextDelta:
    """Represents streamed text emitted by an agent."""

    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    """Represents the start of a tool call."""

    call_id: str
    tool_name: str


@dataclass(frozen=True)
class ToolCallFinished:
    """Represents the end of a tool call."""

    call_id: str


type ChatEvent = TextDelta | ToolCallStarted | ToolCallFinished
