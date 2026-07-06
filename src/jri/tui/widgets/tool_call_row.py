from typing import TYPE_CHECKING, ClassVar

from textual.widgets import Static

from jri.tui import constants as c

if TYPE_CHECKING:
    from textual.timer import Timer


class ToolCallRow(Static):
    SPINNER_FRAMES: ClassVar[tuple[str, ...]] = (
        "⠋",
        "⠙",
        "⠹",
        "⠸",
        "⠼",
        "⠴",
        "⠦",
        "⠧",
        "⠇",
        "⠏",
    )

    def __init__(self, tool_name: str, *, is_complete: bool = False) -> None:
        super().__init__(classes=c.TOOL_CALL_ROW_CLASSES)
        self.tool_name: str = tool_name
        self.frame_idx: int = 0
        self.is_complete: bool = is_complete
        self.spinner_timer: Timer | None = None

    def on_mount(self) -> None:
        self.update_copy()
        if self.is_complete:
            return
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
        prefix = (
            "⚙︎" if self.is_complete else self.SPINNER_FRAMES[self.frame_idx]
        )
        self.update(f"{prefix} {self.tool_name}")
