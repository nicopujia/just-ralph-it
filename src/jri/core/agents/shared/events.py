from dataclasses import dataclass


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    call_id: str
    tool_name: str


@dataclass(frozen=True)
class ToolCallFinished:
    call_id: str


type ChatEvent = TextDelta | ToolCallStarted | ToolCallFinished
