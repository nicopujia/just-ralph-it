from typing import ClassVar, Literal, override

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from jri.tui import copy, styles

# The dialog gives this answer to its caller. The caller stops the run only for `stop`.
type RunCancellationAnswer = Literal["stop", "keep"]


# A run takes a long time, and a stop cannot be undone. Ask for an answer here instead of accepting a key press.
class RunCancellationDialog(ModalScreen[RunCancellationAnswer]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss('keep')", copy.RUN_CANCELLATION_DECLINE, show=False)
    ]

    @override
    def compose(self) -> ComposeResult:
        with Vertical(id=styles.RUN_CANCELLATION_DIALOG_ID):
            yield Static(copy.RUN_CANCELLATION_QUESTION)
            # The first button takes the focus. Offer the answer that leaves the run as it is.
            with Horizontal(classes=styles.RUN_CANCELLATION_ANSWERS_CLASSES):
                yield Button(copy.RUN_CANCELLATION_DECLINE, compact=True)
                yield Button(copy.RUN_CANCELLATION_CONFIRM, id=styles.RUN_CANCELLATION_CONFIRM_BUTTON_ID, compact=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss("stop" if event.button.id == styles.RUN_CANCELLATION_CONFIRM_BUTTON_ID else "keep")
