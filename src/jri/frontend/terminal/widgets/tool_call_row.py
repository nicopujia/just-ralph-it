from textual.widgets import Static

from jri.frontend.terminal import constants as c


class ToolCallRow(Static):
    """Render a tool call row with spinner state while loading."""

    SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, tool_name: str, *, is_complete: bool = False) -> None:
        super().__init__(classes=c.TOOL_CALL_ROW_CLASSES)
        self.tool_name = tool_name
        self.frame_idx = 0
        self.is_complete = is_complete
        self.spinner_timer = None

    def on_mount(self) -> None:
        """Start the spinner when the row mounts."""

        self.update_copy()
        if not self.is_complete:
            self.spinner_timer = self.set_interval(0.08, self.advance_spinner)

    def on_unmount(self) -> None:
        """Stop the spinner when the row unmounts."""

        if self.spinner_timer is not None:
            self.spinner_timer.stop()

    def mark_complete(self) -> None:
        """Mark the tool call as complete."""

        self.is_complete = True
        if self.spinner_timer is not None:
            self.spinner_timer.stop()
            self.spinner_timer = None
        self.update_copy()

    def advance_spinner(self) -> None:
        """Advance the spinner while the call is running."""

        if self.is_complete:
            return
        self.frame_idx = (self.frame_idx + 1) % len(self.SPINNER_FRAMES)
        self.update_copy()

    def update_copy(self) -> None:
        """Refresh the rendered row text."""

        prefix = "⚙︎" if self.is_complete else self.SPINNER_FRAMES[self.frame_idx]
        self.update(f"{prefix} {self.tool_name}")
