from dataclasses import dataclass, field

from textual.containers import Vertical
from textual.widgets import Markdown

from .widgets import ToolCallRow


@dataclass
class InterviewerTurnState:
    container: Vertical
    placeholder: Markdown | None
    active_markdown: Markdown | None = None
    active_markdown_text: str = ""
    tool_rows: dict[str, ToolCallRow] = field(default_factory=dict)
