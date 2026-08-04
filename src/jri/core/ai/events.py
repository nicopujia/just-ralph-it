from dataclasses import dataclass

type ChatEvent = ReasoningDelta | TextDelta | ToolCallStarted | ToolCallFinished


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


@dataclass(frozen=True)
class ToolCallFinished:
    call_id: str
    label: str
    depth: int = 0
