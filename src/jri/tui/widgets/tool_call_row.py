from time import monotonic

from textual.widgets import Static

from jri.core.ai import DEFAULT_SYMBOL
from jri.tui import styles


class ToolCallRow(Static):
    """Render a tool call row with spinner state while loading."""

    SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    # Below this, the elapsed time is noise rather than reassurance.
    MIN_ELAPSED_SECONDS = 3

    def __init__(self, label: str, *, symbol: str = DEFAULT_SYMBOL, is_complete: bool = False, depth: int = 0) -> None:
        super().__init__(classes=styles.TOOL_CALL_ROW_CLASSES)
        self.styles.padding = (0, 2, 0, 2 + depth * 2)
        self.label = label
        self.symbol = symbol
        self.depth = depth
        self.frame_index = 0
        self.is_complete = is_complete
        self.started_at = monotonic()
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

    def mark_complete(self, label: str) -> None:
        """Mark the tool call as complete."""

        self.is_complete = True
        self.label = label
        if self.spinner_timer is not None:
            self.spinner_timer.stop()
            self.spinner_timer = None
        self.update_copy()

    def advance_spinner(self) -> None:
        """Advance the spinner while the call is running."""

        if self.is_complete:
            return
        self.frame_index = (self.frame_index + 1) % len(self.SPINNER_FRAMES)
        self.update_copy()

    def update_copy(self) -> None:
        """Refresh the rendered row text."""

        if self.is_complete:
            self.update(f"{self.symbol} {self.label}")
            return
        elapsed = int(monotonic() - self.started_at)
        suffix = f" [dim]{elapsed // 60}m {elapsed % 60:02d}s[/dim]" if elapsed >= self.MIN_ELAPSED_SECONDS else ""
        self.update(f"{self.SPINNER_FRAMES[self.frame_index]} {self.label}{suffix}")
