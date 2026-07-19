from dataclasses import dataclass
from typing import override

from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea


class MessageInput(TextArea):
    BINDINGS = (
        Binding("enter", "submit", "Send message", show=False, priority=True),
        Binding("shift+enter,ctrl+j", "insert_newline", "Insert newline", show=False, priority=True),
        Binding("ctrl+shift+z", "redo", "Redo", show=False),
    )

    @dataclass
    class Submitted(Message):
        message_input: "MessageInput"
        value: str

        @property
        @override
        def control(self) -> "MessageInput":
            return self.message_input

    def action_insert_newline(self) -> None:
        self.insert("\n")

    def action_submit(self) -> None:
        self.post_message(self.Submitted(self, self.text))

    def on_blur(self) -> None:
        self.focus()
