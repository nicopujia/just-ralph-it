from dataclasses import dataclass


@dataclass(frozen=True)
class ReasoningDelta:
    """Represents streamed reasoning summary text."""

    text: str


@dataclass(frozen=True)
class TextDelta:
    """Represents streamed text emitted by an agent."""

    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    """Represents the start of a tool call."""

    call_id: str
    label: str
    symbol: str
    depth: int = 0


@dataclass(frozen=True)
class ToolCallFinished:
    """Represents the end of a tool call."""

    call_id: str
    label: str
    depth: int = 0


@dataclass(frozen=True)
class ModelIterationCompleted:
    """Signal that one model response was added to the agent context."""


type ChatEvent = ReasoningDelta | TextDelta | ToolCallStarted | ToolCallFinished
