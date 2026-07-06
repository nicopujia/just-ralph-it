from typing import ClassVar, override

from openai import OpenAIError
from textual import work
from textual.app import App as TextualApp
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.reactive import Reactive
from textual.widgets import Header, Markdown, Static
from textual.worker import Worker

from jri.core.agents.shared import (
    ChatEvent,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from jri.core.exceptions import ConfigurationError
from jri.core.service import Service

from . import constants as c
from .states import InterviewerTurnState
from .utils import detect_system_theme, get_config_error_help_message
from .widgets import MessageInput, ToolCallRow


def main() -> None:
    try:
        app = App()
        app.run()
    except ConfigurationError as error:
        print(get_config_error_help_message(error))
        raise SystemExit(1) from error


class App(TextualApp[None]):
    TITLE: str | None = c.TITLE_COPY
    CSS: ClassVar[str] = c.STYLESHEET
    theme: Reactive[str] = Reactive(c.THEME_DEFAULT)
    worker: Worker[None] | None = None

    def __init__(self, service: Service | None = None) -> None:
        super().__init__()
        self.service: Service = service or Service()
        self.is_interviewer_generating: bool = False

    @override
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id=c.MESSAGES_CONTAINER_ID):
            yield Static()
        yield MessageInput(
            id=c.MESSAGE_INPUT_ID,
            placeholder=c.MESSAGE_INPUT_INITIAL_PLACEHOLDER_COPY,
        )

    # --- Event handlers --------------------------------------------- #

    async def on_mount(self) -> None:
        await self.restore_history()
        self.theme = detect_system_theme()
        self.set_focus(self.query_one(f"#{c.MESSAGE_INPUT_ID}", MessageInput))

    async def on_message_input_submitted(
        self,
        event: MessageInput.Submitted,
    ) -> None:
        if self.is_interviewer_generating:
            return

        user_message = event.value.strip()

        if not user_message:
            event.message_input.text = ""
            return

        self.is_interviewer_generating = True
        event.message_input.text = ""
        event.message_input.placeholder = c.MESSAGE_INPUT_PLACEHOLDER_COPY

        messages_container = self.query_one(
            f"#{c.MESSAGES_CONTAINER_ID}",
            VerticalScroll,
        )

        user_message_widget = Static(
            user_message,
            classes=c.USER_MESSAGE_CLASSES,
        )
        interviewer_turn = Vertical(classes=c.INTERVIEWER_TURN_CLASSES)
        thinking_widget = Markdown(
            c.INTERVIEWER_THINKING_COPY,
            classes=c.INTERVIEWER_MESSAGE_CLASSES,
        )
        turn_state = InterviewerTurnState(
            container=interviewer_turn,
            placeholder=thinking_widget,
        )

        await messages_container.mount(user_message_widget)
        await messages_container.mount(interviewer_turn)
        await interviewer_turn.mount(thinking_widget)

        messages_container.anchor()
        self.worker = self.send_message(
            user_message,
            turn_state,
        )

    # --- Workers ---------------------------------------------------- #

    @work(thread=True, exclusive=True)
    def send_message(
        self,
        user_message: str,
        turn_state: InterviewerTurnState,
    ) -> None:
        has_text_response = False
        failed = False
        try:
            for chat_event in self.service.chat(user_message):
                if isinstance(chat_event, TextDelta) and chat_event.text:
                    has_text_response = True
                self.call_from_thread(
                    self.render_chat_event,
                    turn_state,
                    chat_event,
                )
        except (OpenAIError, RuntimeError) as error:
            failed = True
            self.call_from_thread(
                self.render_interviewer_status,
                turn_state,
                c.INTERIVEWER_ERROR_COPY % error,
            )
        finally:
            if not has_text_response and not failed:
                self.call_from_thread(
                    self.render_interviewer_status,
                    turn_state,
                    c.INTERVIEWER_NO_RESPONSE_COPY,
                )
            self.call_from_thread(self.reset_message_input)

    # --- Callbacks -------------------------------------------------- #

    def reset_message_input(self) -> None:
        self.worker = None
        self.is_interviewer_generating = False
        message_input_widget = self.query_one(
            f"#{c.MESSAGE_INPUT_ID}",
            MessageInput,
        )
        self.set_focus(message_input_widget)

    def anchor_messages(self) -> None:
        messages_container = self.query_one(
            f"#{c.MESSAGES_CONTAINER_ID}",
            VerticalScroll,
        )
        messages_container.anchor()

    async def render_chat_event(
        self,
        turn_state: InterviewerTurnState,
        chat_event: ChatEvent,
    ) -> None:
        if turn_state.placeholder is not None:
            await turn_state.placeholder.remove()
            turn_state.placeholder = None

        match chat_event:
            case TextDelta(text=text):
                await self.append_interviewer_text(turn_state, text)
            case ToolCallStarted(call_id=call_id, tool_name=tool_name):
                turn_state.active_markdown = None
                turn_state.active_markdown_text = ""
                turn_state.tool_rows[call_id] = ToolCallRow(tool_name)
                await turn_state.container.mount(turn_state.tool_rows[call_id])
                self.anchor_messages()
            case ToolCallFinished(call_id=call_id):
                tool_row = turn_state.tool_rows.get(call_id)
                if tool_row is not None:
                    tool_row.mark_complete()
                    self.anchor_messages()

    async def render_interviewer_status(
        self,
        turn_state: InterviewerTurnState,
        content: str,
    ) -> None:
        if turn_state.placeholder is not None:
            await turn_state.placeholder.update(content)
            self.anchor_messages()
            return

        turn_state.active_markdown = None
        turn_state.active_markdown_text = ""
        status_widget = Markdown(
            content,
            classes=c.INTERVIEWER_MESSAGE_CLASSES,
        )
        await turn_state.container.mount(status_widget)
        self.anchor_messages()

    # --- Helpers ---------------------------------------------------- #

    async def append_interviewer_text(
        self,
        turn_state: InterviewerTurnState,
        text: str,
    ) -> None:
        if not text:
            return

        if turn_state.active_markdown is None:
            turn_state.active_markdown = Markdown(
                "",
                classes=c.INTERVIEWER_MESSAGE_CLASSES,
            )
            turn_state.active_markdown_text = ""
            await turn_state.container.mount(turn_state.active_markdown)

        turn_state.active_markdown_text += text
        await turn_state.active_markdown.update(
            turn_state.active_markdown_text,
        )
        self.anchor_messages()

    async def restore_history(self) -> None:
        messages = self.query_one(
            f"#{c.MESSAGES_CONTAINER_ID}",
            VerticalScroll,
        )
        for item in self.service.restore() or []:
            if item.type == "user":
                await messages.mount(
                    Static(item.text, classes=c.USER_MESSAGE_CLASSES),
                )
                continue

            interviewer_turn = Vertical(classes=c.INTERVIEWER_TURN_CLASSES)
            await messages.mount(interviewer_turn)
            await interviewer_turn.mount(
                ToolCallRow(item.text, is_complete=True)
                if item.type == "tool"
                else Markdown(
                    item.text,
                    classes=c.INTERVIEWER_MESSAGE_CLASSES,
                ),
            )
        messages.scroll_end(animate=False)


if __name__ == "__main__":
    main()
