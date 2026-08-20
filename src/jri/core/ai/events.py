from dataclasses import dataclass
from typing import Literal

type AgentEvent = ReasoningDelta | TextDelta | ToolCallStarted | ToolCallFinished
type Ending = Literal["replied", "empty", "stopped", "failed", "refused", "unavailable", "exhausted", "blocked"]
type Outcome = Literal["done", "empty", "stopped", "failed"]
type TurnEvent = AgentEvent | TurnFinished


@dataclass(frozen=True)
class ReasoningDelta:
    text: str


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    call_id: str
    label: str
    symbol: str
    depth: int = 0
    # Store the call age when this event occurs. A later window can replay events from another process.
    # The row age starts when the call starts, not when the event reaches the window.
    age: float = 0.0


# A finished call must state its outcome. Do not use a default outcome for a closed row.
@dataclass(frozen=True)
class ToolCallFinished:
    call_id: str
    label: str
    outcome: Outcome
    detail: str = ""
    depth: int = 0


# This is the final event of a turn. Agents, tools, and workflows cannot yield it because it is not an `AgentEvent`.
# Only the conversation can declare that a turn is complete.
@dataclass(frozen=True)
class TurnFinished:
    ending: Ending
    detail: str = ""
