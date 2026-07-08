from dataclasses import dataclass

from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea


class MessageInput(TextArea):
    BINDINGS = (
        Binding("enter", "submit", "Send message", show=False, priority=True),
        Binding("shift+enter", "insert_newline", "Insert newline", show=False, priority=True),
    )

    @dataclass
    class Submitted(Message):
        message_input: "MessageInput"
        value: str

        @property
        def control(self) -> "MessageInput":
            return self.message_input

    def action_insert_newline(self) -> None:
        self.insert("\n")

    def action_submit(self) -> None:
        self.post_message(self.Submitted(self, self.text))
