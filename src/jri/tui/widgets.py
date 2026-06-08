from dataclasses import dataclass
from typing import ClassVar, override

from textual.binding import Binding, BindingType
from textual.message import Message
from textual.widgets import TextArea


class MessageInput(TextArea):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding(
            "enter",
            "submit",
            "Send message",
            show=False,
            priority=True,
        ),
        Binding(
            "shift+enter",
            "insert_newline",
            "Insert newline",
            show=False,
            priority=True,
        ),
    ]

    @dataclass
    class Submitted(Message):
        message_input: "MessageInput"
        value: str

        @property
        @override
        def control(self) -> "MessageInput":
            return self.message_input

    def action_insert_newline(self) -> None:
        _edit_result = self.insert("\n")

    def action_submit(self) -> None:
        _posted = self.post_message(self.Submitted(self, self.text))
