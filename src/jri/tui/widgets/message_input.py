from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, override

from textual.binding import Binding
from textual.message import Message
from textual.reactive import Reactive
from textual.widgets import TextArea

from jri.tui import copy

if TYPE_CHECKING:
    from textual.timer import Timer


class MessageInput(TextArea):
    CHORD_TIMEOUT = 1.0
    BINDINGS = (
        Binding("enter", "submit", copy.SEND_MESSAGE, show=False, priority=True),
        Binding("shift+enter,ctrl+j", "insert_newline", copy.INSERT_NEWLINE, show=False, priority=True),
        Binding("ctrl+x", "message_history", copy.MESSAGE_HISTORY, priority=True),
        Binding("u", "previous_message", copy.UNDO_MESSAGE, key_display=copy.UNDO_MESSAGE_KEY, priority=True),
        Binding("r", "next_message", copy.REDO_MESSAGE, key_display=copy.REDO_MESSAGE_KEY, priority=True),
        Binding("t", "retry_message", copy.RETRY, key_display=copy.RETRY_KEY, priority=True),
        Binding("j", "ralph", copy.RALPH_BUTTON, key_display=copy.RALPH_KEY, priority=True),
        Binding("ctrl+shift+z", "redo", copy.REDO, show=False),
    )
    is_ralph_ready: Reactive[bool] = Reactive(default=False, bindings=True)
    is_retry_ready: Reactive[bool] = Reactive(default=False, bindings=True)
    is_turn_active: Reactive[bool] = Reactive(default=False, bindings=True)
    _is_chord_open: Reactive[bool] = Reactive(default=False, bindings=True)

    @dataclass
    class Submitted(Message):
        message_input: "MessageInput"
        value: str
        history_index: int | None

        @property
        @override
        def control(self) -> "MessageInput":
            return self.message_input

    @dataclass
    class HistoryRequested(Message):
        message_input: "MessageInput"
        direction: Literal["previous", "next"]

    class RetryRequested(Message):
        pass

    class RalphRequested(Message):
        pass

    def __init__(self, messages: Iterable[str] = (), *, id_: str | None = None, placeholder: str = "") -> None:
        super().__init__(id=id_, placeholder=placeholder)
        self._messages = list(messages)
        self._message_index = len(self._messages)
        self._draft = ""
        self._chord_timer: Timer | None = None

    def action_insert_newline(self) -> None:
        self.insert("\n")

    @override
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Offer each shortcut only while the user can reach it.

        Leaving the chord endings disabled outside the chord keeps
        their keys ordinary text, so typing them never routes through
        an action. Inside the chord, each ending shows up only where it
        has something to do. Ralphing stays visible but disabled while
        its button is up, to advertise the chord that reaches it.

        Returns:
            Whether the action may run for this key press.
        """

        if action == "message_history":
            return not self.is_turn_active and not self._is_chord_open
        if action == "ralph" and not self._is_chord_open:
            return None if self.is_ralph_ready and not self.is_turn_active else False
        if action == "previous_message":
            return self._is_chord_open and self.history_index > 0
        if action == "next_message":
            return self._is_chord_open and self.history_index < self.message_count
        if action == "retry_message":
            return self._is_chord_open and self.is_retry_ready
        if action == "ralph":
            return self._is_chord_open and self.is_ralph_ready
        return super().check_action(action, parameters)

    def action_message_history(self) -> None:
        self._close_chord()
        self._is_chord_open = True
        self._chord_timer = self.set_timer(self.CHORD_TIMEOUT, self._close_chord)

    def action_previous_message(self) -> None:
        self._close_chord()
        self.post_message(self.HistoryRequested(self, "previous"))

    def action_next_message(self) -> None:
        self._close_chord()
        self.post_message(self.HistoryRequested(self, "next"))

    def action_retry_message(self) -> None:
        self._close_chord()
        self.post_message(self.RetryRequested())

    def action_ralph(self) -> None:
        self._close_chord()
        self.post_message(self.RalphRequested())

    @property
    def history_index(self) -> int:
        """Return the selected conversation-wide message position."""

        return self._message_index

    @property
    def message_count(self) -> int:
        """Return the number of remembered user messages."""

        return len(self._messages)

    def select_previous(self) -> None:
        """Select the previous user message."""

        if self._message_index == len(self._messages):
            self._draft = self.text
        if self._message_index > 0:
            self._message_index -= 1
            self._load(self._messages[self._message_index])

    def select_next(self) -> None:
        """Select the next user message or saved draft."""

        if self._message_index < len(self._messages):
            self._message_index += 1
            self._load(
                self._messages[self._message_index] if self._message_index < len(self._messages) else self._draft
            )

    def action_submit(self) -> None:
        history_index = self._message_index if self._message_index < len(self._messages) else None
        self.post_message(self.Submitted(self, self.text, history_index))

    def remember(self, value: str) -> None:
        """Remember one accepted message and clear the input."""

        del self._messages[self._message_index :]
        self._messages.append(value)
        self._message_index = len(self._messages)
        self.text = ""

    def on_blur(self) -> None:
        self.focus()

    def _close_chord(self) -> None:
        if self._chord_timer is not None:
            self._chord_timer.stop()
            self._chord_timer = None
        self._is_chord_open = False

    def _load(self, value: str) -> None:
        self.text = value
        self.move_cursor(self.document.end)
