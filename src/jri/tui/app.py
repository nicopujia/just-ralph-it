from typing import ClassVar, override

from openai import OpenAIError
from textual import work
from textual.app import App as TextualApp
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.reactive import Reactive
from textual.widgets import Header, Markdown, Static
from textual.worker import Worker

from jri.core.exceptions import ConfigurationError
from jri.core.service import Service

from .constants import (
    INTERIVEWER_ERROR_COPY,
    INTERVIEWER_MESSAGE_CLASSES,
    INTERVIEWER_NO_RESPONSE_COPY,
    INTERVIEWER_THINKING_COPY,
    MESSAGE_INPUT_ID,
    MESSAGE_INPUT_PLACEHOLDER_COPY,
    MESSAGES_CONTAINER_ID,
    STYLESHEET,
    THEME_DEFAULT,
    THEME_SYNC_INTERVAL_SECONDS,
    TITLE_COPY,
    USER_MESSAGE_CLASSES,
)
from .utils import detect_system_theme, get_config_error_help_message
from .widgets import MessageInput


def main() -> None:
    try:
        app = App()
        app.run()
    except ConfigurationError as error:
        print(get_config_error_help_message(error))
        raise SystemExit(1) from error


class App(TextualApp[None]):
    TITLE: str | None = TITLE_COPY
    CSS: ClassVar[str] = STYLESHEET
    theme: Reactive[str] = Reactive(THEME_DEFAULT)
    worker: Worker[None] | None = None

    def __init__(self, service: Service | None = None) -> None:
        super().__init__()
        self.theme = detect_system_theme()
        self.service: Service = service or Service()

    @override
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll(id=MESSAGES_CONTAINER_ID):
            yield Static()
        yield MessageInput(
            id=MESSAGE_INPUT_ID,
            placeholder=MESSAGE_INPUT_PLACEHOLDER_COPY,
        )

    # --- Event handlers --------------------------------------------- #

    async def on_mount(self) -> None:
        _timer = self.set_interval(
            THEME_SYNC_INTERVAL_SECONDS,
            self.sync_system_theme,
        )
        self.set_focus(self.query_one(f"#{MESSAGE_INPUT_ID}", MessageInput))

    async def on_message_input_submitted(
        self,
        event: MessageInput.Submitted,
    ) -> None:
        user_message = event.value.strip()

        if not user_message:
            event.message_input.text = ""
            return

        event.message_input.text = ""
        event.message_input.disabled = True

        messages_container = self.query_one(
            f"#{MESSAGES_CONTAINER_ID}",
            VerticalScroll,
        )

        user_message_widget = Static(
            user_message,
            classes=USER_MESSAGE_CLASSES,
        )
        interviewer_message_widget = Markdown(
            INTERVIEWER_THINKING_COPY,
            classes=INTERVIEWER_MESSAGE_CLASSES,
        )

        await messages_container.mount(user_message_widget)
        await messages_container.mount(interviewer_message_widget)

        messages_container.anchor()
        self.worker = self.send_message(
            user_message,
            interviewer_message_widget,
        )

    # --- Workers ---------------------------------------------------- #

    @work(thread=True, exclusive=True)
    def send_message(
        self,
        user_message: str,
        interviewer_message_widget: Markdown,
    ) -> None:
        answer = ""
        failed = False
        try:
            for answer_chunk in self.service.send_message(user_message):
                answer += answer_chunk
                self.call_from_thread(
                    self.update_interviewer_message_widget,
                    interviewer_message_widget,
                    answer,
                )
        except OpenAIError as error:
            failed = True
            self.call_from_thread(
                self.update_interviewer_message_widget,
                interviewer_message_widget,
                INTERIVEWER_ERROR_COPY % error,
            )
        finally:
            if not answer and not failed:
                self.call_from_thread(
                    self.update_interviewer_message_widget,
                    interviewer_message_widget,
                    INTERVIEWER_NO_RESPONSE_COPY,
                )
            self.call_from_thread(self.focus_message_input)

    # --- Callbacks -------------------------------------------------- #

    def focus_message_input(self) -> None:
        message_input_widget = self.query_one(
            f"#{MESSAGE_INPUT_ID}",
            MessageInput,
        )
        message_input_widget.disabled = False
        self.set_focus(message_input_widget)

    def sync_system_theme(self) -> None:
        system_theme = detect_system_theme()
        if self.theme != system_theme:
            self.theme = system_theme

    @staticmethod
    async def update_interviewer_message_widget(
        widget: Markdown,
        new_content: str,
    ) -> None:
        await widget.update(new_content)


if __name__ == "__main__":
    main()
