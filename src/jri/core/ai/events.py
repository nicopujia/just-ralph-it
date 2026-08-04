"""Provider-agnostic events the UI renders.

Kept independent of the OpenAI stream types: one `ReasoningDelta`
covers three reasoning delta events, the tool call events carry a
label, symbol, and nesting depth no provider type has, and workflows
emit them with no model call behind them.
"""

from dataclasses import dataclass

type ChatEvent = ReasoningDelta | TextDelta | ToolCallStarted | ToolCallFinished


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
