from openai import OpenAIError
from textual import work
from textual.app import App as TextualApp
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.reactive import Reactive
from textual.widgets import Header, Markdown, Static

from jri.core.agents import ChatEvent, TextDelta, ToolCallFinished, ToolCallStarted
from jri.core.service import Service

from . import constants as c
from .states import InterviewerTurnState
from .utils import detect_system_theme
from .widgets import MessageInput, ToolCallRow


class App(TextualApp[None]):
    """Render the terminal UI for the interviewer chat."""

    TITLE = c.TITLE_COPY
    CSS = c.STYLESHEET
    theme = Reactive(c.THEME_DEFAULT)

    def __init__(self, service: Service) -> None:
        super().__init__()
        self.service = service
        self.is_interviewer_generating = False
        self.worker = None
        self.messages_container = VerticalScroll(id=c.MESSAGES_CONTAINER_ID)
        self.message_input = MessageInput(id=c.MESSAGE_INPUT_ID, placeholder=c.MESSAGE_INPUT_INITIAL_PLACEHOLDER_COPY)

    def compose(self) -> ComposeResult:
        """Compose the terminal layout.

        Yields:
            The widgets that make up the terminal UI.
        """

        yield Header(show_clock=True)
        with self.messages_container:
            yield Static()
        yield self.message_input

    # --- Event handlers --------------------------------------------- #

    async def on_mount(self) -> None:
        """Restore history and initialize the app state."""

        await self.restore_history()
        self.theme = detect_system_theme()
        self.set_focus(self.message_input)

    async def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        """Send a submitted user message to the interviewer."""

        if self.is_interviewer_generating:
            return

        user_message = event.value.strip()

        if not user_message:
            event.message_input.text = ""
            return

        self.is_interviewer_generating = True
        event.message_input.text = ""
        event.message_input.placeholder = c.MESSAGE_INPUT_PLACEHOLDER_COPY

        interviewer_turn = Vertical(classes=c.INTERVIEWER_TURN_CLASSES)
        placeholder = Markdown(c.INTERVIEWER_THINKING_COPY, classes=c.INTERVIEWER_MESSAGE_CLASSES)
        turn_state = InterviewerTurnState(container=interviewer_turn, placeholder=placeholder)

        await self.messages_container.mount(Static(user_message, classes=c.USER_MESSAGE_CLASSES))
        await self.messages_container.mount(interviewer_turn)
        await interviewer_turn.mount(placeholder)

        self.messages_container.anchor()
        self.worker = self.send_message(user_message, turn_state)

    # --- Workers ---------------------------------------------------- #

    @work(thread=True, exclusive=True)
    def send_message(self, user_message: str, turn_state: InterviewerTurnState) -> None:
        """Stream interviewer events for a user message."""

        status_copy = c.INTERVIEWER_NO_RESPONSE_COPY
        try:
            for chat_event in self.service.chat(user_message):
                if isinstance(chat_event, TextDelta) and chat_event.text:
                    status_copy = None
                self.call_from_thread(self.render_chat_event, turn_state, chat_event)
        except (OpenAIError, RuntimeError) as error:
            status_copy = c.INTERVIEWER_ERROR_COPY.format(error=error)
        finally:
            if status_copy is not None:
                self.call_from_thread(self.render_interviewer_status, turn_state, status_copy)
            self.call_from_thread(self.reset_message_input)

    # --- Callbacks -------------------------------------------------- #

    def reset_message_input(self) -> None:
        """Reset the input state after a worker finishes."""

        self.worker = None
        self.is_interviewer_generating = False
        self.set_focus(self.message_input)

    async def render_chat_event(self, turn_state: InterviewerTurnState, chat_event: ChatEvent) -> None:
        """Render one streamed event into the current turn."""

        if turn_state.placeholder is not None:
            await turn_state.placeholder.remove()
            turn_state.placeholder = None

        match chat_event:
            case TextDelta(text=text):
                await self.append_interviewer_text(turn_state, text)
            case ToolCallStarted(call_id=call_id, label=label, symbol=symbol):
                turn_state.active_markdown = None
                turn_state.active_markdown_text = ""
                turn_state.tool_rows[call_id] = ToolCallRow(label, symbol=symbol)
                await turn_state.container.mount(turn_state.tool_rows[call_id])
                self.messages_container.anchor()
            case ToolCallFinished(call_id=call_id, label=label):
                turn_state.tool_rows[call_id].mark_complete(label)
                self.messages_container.anchor()

    async def render_interviewer_status(self, turn_state: InterviewerTurnState, content: str) -> None:
        """Render a status message for the interviewer turn."""

        if turn_state.placeholder is None:
            turn_state.active_markdown = None
            turn_state.active_markdown_text = ""
            await turn_state.container.mount(Markdown(content, classes=c.INTERVIEWER_MESSAGE_CLASSES))
        else:
            await turn_state.placeholder.update(content)
        self.messages_container.anchor()

    # --- Helpers ---------------------------------------------------- #

    async def append_interviewer_text(self, turn_state: InterviewerTurnState, text: str) -> None:
        """Append streamed text to the active message block."""

        if turn_state.active_markdown is None:
            turn_state.active_markdown = Markdown("", classes=c.INTERVIEWER_MESSAGE_CLASSES)
            await turn_state.container.mount(turn_state.active_markdown)

        turn_state.active_markdown_text += text
        await turn_state.active_markdown.update(turn_state.active_markdown_text)
        self.messages_container.anchor()

    async def restore_history(self) -> None:
        """Rebuild the visible chat history from persisted items."""

        items = self.service.restore()
        if items:
            self.message_input.placeholder = c.MESSAGE_INPUT_PLACEHOLDER_COPY

        interviewer_turn = None
        for item in items:
            if item.type == "user":
                await self.messages_container.mount(Static(item.text, classes=c.USER_MESSAGE_CLASSES))
                interviewer_turn = None
                continue

            if interviewer_turn is None:
                interviewer_turn = Vertical(classes=c.INTERVIEWER_TURN_CLASSES)
                await self.messages_container.mount(interviewer_turn)
            await interviewer_turn.mount(
                ToolCallRow(item.text, symbol=item.symbol or "⚙︎", is_complete=True)
                if item.type == "tool"
                else Markdown(item.text, classes=c.INTERVIEWER_MESSAGE_CLASSES)
            )
        self.messages_container.scroll_end(animate=False)
