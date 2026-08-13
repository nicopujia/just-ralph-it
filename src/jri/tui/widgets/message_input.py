from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, override

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.reactive import Reactive
from textual.widgets import TextArea

from jri.tui import copy

# The text area deletes to the end of the line with the key that the app uses for the keymap panel.
TEXT_AREA_CTRL_K_ACTION = "delete_to_end_of_line_or_delete_line"


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
        # Use a separate action, not the text-area `^y` action. The keymap panel then shows a separate row.
        # Otherwise it puts this binding in the shared row without a description.
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
        # The app owns this key and has priority over this text-area action, thus the action is unreachable.
        # Stand it down. The footer lists each key where its first owner declares it, and an entry here would
        # move the app key to this widget and change the footer order when the input takes the focus.
        if action == TEXT_AREA_CTRL_K_ACTION:
            return False
        if action == "open_shortcuts":
            return not self.is_turn_active and not self.is_shortcuts_open
        # Escape closes shortcuts only when they are open. When closed, it lets `esc esc` reach the turn.
        if action == "close_shortcuts":
            return self.is_shortcuts_open
        if action in self.SHORTCUT_ACTIONS:
            # Open shortcuts own their letters. An unavailable action does nothing instead of adding text.
            # Each action checks its availability. Closed shortcuts let the text area use the letters.
            # The keymap must list all product bindings. It shows unavailable bindings instead of hiding them.
            return True if self.is_shortcuts_open else None
        # Open shortcuts own the keyboard. All other bindings stand down. Their keys close the shortcuts.
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

    # A turn from shortcuts, or the message below them, owns Escape. Close shortcuts before the turn can stop.
    def watch_is_turn_active(self) -> None:
        if self.is_turn_active:
            self.is_shortcuts_open = False

    # Do not add input to the draft while shortcuts are open. The user opened them to use commands, not type text.
    # A terminal sends input as keys or, with bracketed paste, as one `Paste` event.
    # Escape uses its binding and can bubble. Stop other input before text-area handlers or bindings receive it.
    @override
    async def _on_key(self, event: events.Key) -> None:
        if self.is_shortcuts_open and event.key != "escape":
            self._refuse(event)

    @override
    async def _on_paste(self, event: events.Paste) -> None:
        if self.is_shortcuts_open:
            self._refuse(event)

    def _load(self, value: str) -> None:
        self.text = value
        self.move_cursor(self.document.end)

    def _refuse(self, event: events.Event) -> None:
        self.is_shortcuts_open = False
        event.prevent_default()
        event.stop()
