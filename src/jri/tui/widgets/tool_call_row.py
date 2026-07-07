from typing import ClassVar

from textual.widgets import Static

from jri.tui import constants as c


class ToolCallRow(Static):
    SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    TOOL_LABELS: ClassVar[dict[str, str]] = dict.fromkeys(
        (
            "add_feature",
            "add_note",
            "archive_note",
            "resolve_question",
            "revise_note",
            "set_feature_brief",
            "set_project_brief",
        ),
        "Updating notes",
    ) | {"explore": "Exploring", "read_notes": "Checking notes", "switch_focus": "Organizing notes"}

    def __init__(self, tool_name: str, *, is_complete: bool = False) -> None:
        super().__init__(classes=c.TOOL_CALL_ROW_CLASSES)
        self.tool_name = tool_name
        self.frame_idx = 0
        self.is_complete = is_complete
        self.spinner_timer = None

    def on_mount(self) -> None:
        self.update_copy()
        if not self.is_complete:
            self.spinner_timer = self.set_interval(0.08, self.advance_spinner)

    def on_unmount(self) -> None:
        if self.spinner_timer is not None:
            self.spinner_timer.stop()

    def mark_complete(self) -> None:
        self.is_complete = True
        if self.spinner_timer is not None:
            self.spinner_timer.stop()
            self.spinner_timer = None
        self.update_copy()

    def advance_spinner(self) -> None:
        if self.is_complete:
            return
        self.frame_idx = (self.frame_idx + 1) % len(self.SPINNER_FRAMES)
        self.update_copy()

    def update_copy(self) -> None:
        prefix = "⚙︎" if self.is_complete else self.SPINNER_FRAMES[self.frame_idx]
        self.update(f"{prefix} {self.TOOL_LABELS.get(self.tool_name, self.tool_name)}")
