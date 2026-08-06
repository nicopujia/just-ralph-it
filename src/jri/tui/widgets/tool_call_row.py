from time import monotonic

from textual.content import Content
from textual.widgets import Static

from jri.core.ai import DEFAULT_SYMBOL, Outcome
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
        outcome: Outcome = "done",
        detail: str = "",
        depth: int = 0,
    ) -> None:
        super().__init__(classes=styles.TOOL_CALL_ROW_CLASSES)
        self.styles.padding = (0, 2, 0, 2 + depth * 2)
        self.label = label
        self.symbol = symbol
        self.depth = depth
        self.frame_index = 0
        self.is_complete = is_complete
        self.is_stopping = False
        self.outcome: Outcome = outcome
        self.detail = detail
        self.started_at = monotonic()
        self.spinner_timer = None

    def on_mount(self) -> None:
        self.update_copy()
        if not self.is_complete:
            self.spinner_timer = self.set_interval(0.08, self.advance_spinner)

    def on_unmount(self) -> None:
        if self.spinner_timer is not None:
            self.spinner_timer.stop()

    def mark_complete(self, label: str, outcome: Outcome, detail: str = "") -> None:
        self.is_complete = True
        self.label = label
        self.outcome = outcome
        self.detail = detail
        if self.spinner_timer is not None:
            self.spinner_timer.stop()
            self.spinner_timer = None
        self.update_copy()

    def mark_stopping(self) -> None:
        self.is_stopping = True
        self.update_copy()

    def advance_spinner(self) -> None:
        if self.is_complete:
            return
        self.frame_index = (self.frame_index + 1) % len(self.SPINNER_FRAMES)
        self.update_copy()

    def update_copy(self) -> None:
        if self.is_complete:
            self.set_class(self.outcome == "failed", styles.TOOL_CALL_ROW_FAILED_CLASSES)
            symbol, label = _describe_outcome(self.outcome, self.symbol, self.label, self.detail)
            self.update(Content(f"{symbol} {label}"))
            return
        elapsed = int(monotonic() - self.started_at)
        label = copy.TOOL_CALL_STOPPING.format(label=self.label) if self.is_stopping else self.label
        content = Content(f"{self.SPINNER_FRAMES[self.frame_index]} {label}")
        if elapsed >= self.MIN_ELAPSED_SECONDS:
            content = content.append(Content.styled(f" {elapsed // 60}m {elapsed % 60:02d}s", "dim"))
        self.update(content)


# Every outcome is answered here and nowhere else, so no call site ever
# picks a symbol, and one left unanswered is a return type this function
# cannot satisfy.
def _describe_outcome(outcome: Outcome, symbol: str, label: str, detail: str) -> tuple[str, str]:
    match outcome:
        case "done":
            return symbol, label
        case "empty":
            return copy.TOOL_CALL_EMPTY_SYMBOL, label
        case "stopped":
            return copy.TOOL_CALL_STOPPED_SYMBOL, copy.TOOL_CALL_STOPPED.format(label=label)
        case "failed":
            return copy.TOOL_CALL_FAILED_SYMBOL, (
                copy.TOOL_CALL_DETAILED.format(label=label, detail=detail)
                if detail
                else copy.TOOL_CALL_FAILED.format(label=label)
            )
