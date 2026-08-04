from dataclasses import dataclass, field
from threading import Event

from textual.containers import Vertical
from textual.widgets import Button, Markdown

from .widgets import ToolCallRow


@dataclass
class InterviewerTurnState:
    container: Vertical
    placeholder: Markdown | None
    active_markdown: Markdown | None = None
    active_markdown_text: str = ""
    active_reasoning: Markdown | None = None
    active_reasoning_text: str = ""
    tool_rows: dict[str, ToolCallRow] = field(default_factory=dict)
    retry_button: Button | None = None
    follow_bottom: bool = True
    cancelled: Event = field(default_factory=Event)
