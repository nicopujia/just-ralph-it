from dataclasses import dataclass
from typing import Literal

type AgentEvent = ReasoningDelta | TextDelta | ToolCallStarted | ToolCallFinished
type Outcome = Literal["done", "empty", "stopped", "failed"]


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
