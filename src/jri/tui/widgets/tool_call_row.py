from time import monotonic

from textual.content import Content
from textual.widgets import Static

from jri.core.ai import DEFAULT_SYMBOL
from jri.tui import copy, styles


class ToolCallRow(Static):
    SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    # Below this, the elapsed time is noise rather than reassurance.
    MIN_ELAPSED_SECONDS = 3

    def __init__(
        self,
        label: str,
        *,
        symbol: str = DEFAULT_SYMBOL,
        is_complete: bool = False,
        has_failed: bool = False,
        depth: int = 0,
    ) -> None:
        super().__init__(classes=styles.TOOL_CALL_ROW_CLASSES)
        self.styles.padding = (0, 2, 0, 2 + depth * 2)
        self.label = label
        self.symbol = symbol
        self.depth = depth
        self.frame_index = 0
        self.is_complete = is_complete
        self.has_failed = has_failed
        self.started_at = monotonic()
        self.spinner_timer = None

    def on_mount(self) -> None:
        self.update_copy()
        if not self.is_complete:
            self.spinner_timer = self.set_interval(0.08, self.advance_spinner)

    def on_unmount(self) -> None:
        if self.spinner_timer is not None:
            self.spinner_timer.stop()

    def mark_complete(self, label: str, *, has_failed: bool = False) -> None:
        self.is_complete = True
        self.label = label
        self.has_failed = has_failed
        if self.spinner_timer is not None:
            self.spinner_timer.stop()
            self.spinner_timer = None
        self.update_copy()

    def advance_spinner(self) -> None:
        if self.is_complete:
            return
        self.frame_index = (self.frame_index + 1) % len(self.SPINNER_FRAMES)
        self.update_copy()

    def update_copy(self) -> None:
        if self.is_complete:
            self.set_class(self.has_failed, styles.TOOL_CALL_ROW_FAILED_CLASSES)
            symbol = copy.TOOL_CALL_FAILED_SYMBOL if self.has_failed else self.symbol
            label = copy.TOOL_CALL_FAILED.format(label=self.label) if self.has_failed else self.label
            self.update(Content(f"{symbol} {label}"))
            return
        elapsed = int(monotonic() - self.started_at)
        content = Content(f"{self.SPINNER_FRAMES[self.frame_index]} {self.label}")
        if elapsed >= self.MIN_ELAPSED_SECONDS:
            content = content.append(Content.styled(f" {elapsed // 60}m {elapsed % 60:02d}s", "dim"))
        self.update(content)
