from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, override

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.reactive import Reactive
from textual.widgets import TextArea

from jri.tui import copy


class MessageInput(TextArea):
    SHORTCUT_ACTIONS = frozenset({"previous_message", "next_message", "retry_message", "ralph"})
    BINDINGS = (
        Binding("enter", "submit", copy.SEND_MESSAGE, show=False, priority=True),
        Binding("shift+enter,ctrl+j", "insert_newline", copy.INSERT_NEWLINE, show=False, priority=True),
        Binding("ctrl+x", "open_shortcuts", copy.SHORTCUTS, show=False, priority=True),
        Binding("escape", "close_shortcuts", copy.CLOSE_SHORTCUTS, key_display=copy.CLOSE_SHORTCUTS_KEY, show=False),
        Binding(
            "u", "previous_message", copy.UNDO_MESSAGE, key_display=copy.UNDO_MESSAGE_KEY, show=False, priority=True
        ),
        Binding("r", "next_message", copy.REDO_MESSAGE, key_display=copy.REDO_MESSAGE_KEY, show=False, priority=True),
        Binding("t", "retry_message", copy.RETRY, key_display=copy.RETRY_KEY, show=False, priority=True),
        Binding("j", "ralph", copy.RALPH_BUTTON, key_display=copy.RALPH_KEY, show=False, priority=True),
        # Its own action, rather than the one the text area binds ^y to,
        # so the keymap panel gives it a row of its own instead of
        # wrapping it under the row it would share, description-less.
        Binding("ctrl+shift+z", "redo_edit", copy.REDO, show=False),
    )
    is_ralph_ready: Reactive[bool] = Reactive(default=False, bindings=True)
    is_retry_ready: Reactive[bool] = Reactive(default=False, bindings=True)
    is_shortcuts_open: Reactive[bool] = Reactive(default=False, bindings=True)
    is_turn_active: Reactive[bool] = Reactive(default=False, bindings=True)

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

    def action_insert_newline(self) -> None:
        self.insert("\n")

    def action_redo_edit(self) -> None:
        self.redo()

    @override
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "open_shortcuts":
            return not self.is_turn_active and not self.is_shortcuts_open
        # Escape means "leave the shortcuts" only while they are open;
        # closed, the binding steps aside so that `esc esc` reaches the
        # turn it stops.
        if action == "close_shortcuts":
            return self.is_shortcuts_open
        if action in self.SHORTCUT_ACTIONS:
            # While the shortcuts are open their letters belong to them,
            # so an unavailable one does nothing instead of falling
            # through to the text. Each action checks what it needs
            # beforehand. Closed, the shortcuts leave their letters to
            # the text, but the keymap panel still has to list the
            # bindings the product is driven by, so they report
            # themselves as unavailable rather than as absent.
            return True if self.is_shortcuts_open else None
        # Open, the shortcuts own the keyboard, so every other binding
        # this box has stands down and its key closes them instead.
        if self.is_shortcuts_open:
            return False
        return super().check_action(action, parameters)

    def action_open_shortcuts(self) -> None:
        self.is_shortcuts_open = True

    def action_close_shortcuts(self) -> None:
        self.is_shortcuts_open = False

    def action_previous_message(self) -> None:
        self.is_shortcuts_open = False
        if self.history_index > 0:
            self.post_message(self.HistoryRequested(self, "previous"))

    def action_next_message(self) -> None:
        self.is_shortcuts_open = False
        if self.history_index < self.message_count:
            self.post_message(self.HistoryRequested(self, "next"))

    def action_retry_message(self) -> None:
        self.is_shortcuts_open = False
        if self.is_retry_ready:
            self.post_message(self.RetryRequested())

    def action_ralph(self) -> None:
        self.is_shortcuts_open = False
        if self.is_ralph_ready:
            self.post_message(self.RalphRequested())

    @property
    def history_index(self) -> int:
        return self._message_index

    @property
    def message_count(self) -> int:
        return len(self._messages)

    def select_previous(self) -> None:
        if self._message_index == len(self._messages):
            self._draft = self.text
        if self._message_index > 0:
            self._message_index -= 1
            self._load(self._messages[self._message_index])

    def select_next(self) -> None:
        if self._message_index < len(self._messages):
            self._message_index += 1
            self._load(
                self._messages[self._message_index] if self._message_index < len(self._messages) else self._draft
            )

    def action_submit(self) -> None:
        history_index = self._message_index if self._message_index < len(self._messages) else None
        self.post_message(self.Submitted(self, self.text, history_index))

    def remember(self, value: str) -> None:
        del self._messages[self._message_index :]
        self._messages.append(value)
        self._message_index = len(self._messages)
        self.text = ""

    def on_blur(self) -> None:
        self.focus()

    # A turn started from the shortcuts -- or from the message the
    # shortcuts were open over -- takes Escape for itself, so the
    # shortcuts are gone by the time it can be stopped.
    def watch_is_turn_active(self) -> None:
        if self.is_turn_active:
            self.is_shortcuts_open = False

    # No key pressed while the shortcuts are open may reach the draft:
    # the reader who opened them was not typing prose. Escape answers
    # them through its own binding, so it alone bubbles; anything else
    # stops here, ahead of the text area's own handler and of the
    # bindings the key would otherwise bubble into.
    @override
    async def _on_key(self, event: events.Key) -> None:
        if self.is_shortcuts_open and event.key != "escape":
            self.is_shortcuts_open = False
            event.prevent_default()
            event.stop()

    def _load(self, value: str) -> None:
        self.text = value
        self.move_cursor(self.document.end)
