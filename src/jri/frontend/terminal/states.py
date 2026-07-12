from dataclasses import dataclass, field

from textual.containers import Vertical
from textual.widgets import Markdown

from .widgets import ToolCallRow


@dataclass
class InterviewerTurnState:
    """Tracks UI state for one interviewer turn."""

    container: Vertical
    placeholder: Markdown | None
    active_markdown: Markdown | None = None
    active_markdown_text: str = ""
    active_reasoning: Markdown | None = None
    active_reasoning_text: str = ""
    tool_rows: dict[str, ToolCallRow] = field(default_factory=dict)
    follow_bottom: bool = True
