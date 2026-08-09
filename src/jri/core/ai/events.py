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


# A call that finished says how it went, so no default lets a row be
# closed without answering that.
@dataclass(frozen=True)
class ToolCallFinished:
    call_id: str
    label: str
    outcome: Outcome
    detail: str = ""
    depth: int = 0


# The last event of every turn, and the one an agent, a tool or a
# workflow cannot yield: they are typed `AgentEvent`, which does not
# hold it, so only the conversation declares a turn over.
@dataclass(frozen=True)
class TurnFinished:
    ending: Ending
    detail: str = ""
