from collections.abc import Iterable
from dataclasses import dataclass
from time import monotonic
from typing import Literal, override

from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea


class MessageInput(TextArea):
    BINDINGS = (
        Binding("enter", "submit", "Send message", show=False, priority=True),
        Binding("shift+enter,ctrl+j", "insert_newline", "Insert newline", show=False, priority=True),
        Binding("ctrl+x", "message_history", "Message history", show=False, priority=True),
        Binding("u", "previous_message", "Undo message", show=False, priority=True),
        Binding("r", "next_message", "Redo message", show=False, priority=True),
        Binding("t", "retry_message", "Try again", show=False, priority=True),
        Binding("j", "ralph", "Just Ralph It", show=False, priority=True),
        Binding("ctrl+shift+z", "redo", "Redo", show=False),
    )

    def __init__(self, messages: Iterable[str] = (), *, id_: str | None = None, placeholder: str = "") -> None:
        super().__init__(id=id_, placeholder=placeholder)
        self._messages = list(messages)
        self._message_index = len(self._messages)
        self._draft = ""
        self._message_history_at = 0.0

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

    def action_insert_newline(self) -> None:
        self.insert("\n")

    @override
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Offer the chord endings only while the chord is open.

        Leaving them disabled otherwise keeps their keys ordinary text,
        so typing them never routes through an action.

        Returns:
            Whether the action may run for this key press.
        """

        if action in {"previous_message", "next_message", "retry_message", "ralph"}:
            return monotonic() - self._message_history_at <= 1
        return super().check_action(action, parameters)

    def action_message_history(self) -> None:
        self._message_history_at = monotonic()

    def action_previous_message(self) -> None:
        self._message_history_at = 0.0
        self.post_message(self.HistoryRequested(self, "previous"))

    def action_next_message(self) -> None:
        self._message_history_at = 0.0
        self.post_message(self.HistoryRequested(self, "next"))

    def action_retry_message(self) -> None:
        self._message_history_at = 0.0
        self.post_message(self.RetryRequested())

    def action_ralph(self) -> None:
        self._message_history_at = 0.0
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

    def _load(self, value: str) -> None:
        self.text = value
        self.move_cursor(self.document.end)

    def on_blur(self) -> None:
        self.focus()
