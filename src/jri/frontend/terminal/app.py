from collections.abc import Callable, Iterable
from threading import Thread
from time import monotonic
from typing import Any, ClassVar

from openai import OpenAIError
from textual.app import App as TextualApp
from textual.app import ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.command import CommandPalette as TextualCommandPalette
from textual.containers import Vertical, VerticalScroll
from textual.reactive import Reactive
from textual.screen import Screen
from textual.widgets import Header, Markdown, Static

from jri.core.agents import ChatEvent, ReasoningDelta, TextDelta, ToolCallFinished, ToolCallStarted
from jri.core.service import Service

from . import constants as c
from .states import InterviewerTurnState
from .utils import detect_system_theme
from .widgets import MessageInput, ToolCallRow


class CommandPalette(TextualCommandPalette):
    BINDINGS: ClassVar[list[BindingType]] = [
        *TextualCommandPalette.BINDINGS,
        Binding("ctrl+n", "cursor_down", "Next command", show=False),
    ]

    def action_previous_command(self) -> None:
        """Move to the previous command."""

        self._action_command_list("cursor_up")


class App(TextualApp[None]):
    """Render the terminal UI for the interviewer chat."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+k", "toggle_keymap_panel", "Show/hide keymap", show=False, priority=True),
        Binding("ctrl+t", "toggle_reasoning", "Show/hide thinking blocks", show=False, priority=True),
        Binding("escape", "halt_agent", "Stop agent", show=False, priority=True),
    ]
    TITLE = c.TITLE_COPY
    CSS = c.STYLESHEET
    theme = Reactive(c.THEME_DEFAULT)

    def __init__(self, service: Service) -> None:
        super().__init__()
        self.service = service
        self.is_interviewer_generating = False
        self.is_reasoning_visible = False
        self.last_escape_time: float | None = None
        self.active_turn_state: InterviewerTurnState | None = None
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
        self.active_turn_state = turn_state

        await self.messages_container.mount(Static(user_message, classes=c.USER_MESSAGE_CLASSES))
        await self.messages_container.mount(interviewer_turn)
        await interviewer_turn.mount(placeholder)

        self.messages_container.anchor()
        Thread(target=self.send_message, args=(user_message, turn_state), name="interviewer", daemon=True).start()

    def action_command_palette(self) -> None:
        """Show the command palette."""

        if isinstance(self.screen, CommandPalette):
            self.screen.action_previous_command()
        elif self.use_command_palette:
            self.push_screen(CommandPalette(id="--command-palette"))

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """Return application commands available in the palette.

        Yields:
            Commands available in the command palette.
        """

        for command in super().get_system_commands(screen):
            if command.title != "Maximize":
                yield command
        yield SystemCommand(
            "Hide thinking blocks" if self.is_reasoning_visible else "Show thinking blocks",
            "Toggle model's chain-of-thought (reasoning) text blocks.",
            self.action_toggle_reasoning,
        )

    # --- Workers ---------------------------------------------------- #

    def send_message(self, user_message: str, turn_state: InterviewerTurnState) -> None:
        """Stream interviewer events for a user message."""

        status_copy = c.INTERVIEWER_NO_RESPONSE_COPY
        try:
            for chat_event in self.service.chat(user_message):
                if isinstance(chat_event, TextDelta) and chat_event.text:
                    status_copy = None
                self._call_from_thread(self.render_chat_event, turn_state, chat_event)
        except (OpenAIError, RuntimeError) as error:
            status_copy = c.INTERVIEWER_ERROR_COPY.format(error=error)
        finally:
            if status_copy is not None and not turn_state.is_halted:
                self._call_from_thread(self.render_interviewer_status, turn_state, status_copy)
            self._call_from_thread(self.reset_message_input, turn_state)

    def _call_from_thread(self, callback: Callable[..., Any], *args: object) -> None:
        if not self.is_running:
            return
        try:
            self.call_from_thread(callback, *args)
        except RuntimeError:
            if self.is_running:
                raise

    # --- Callbacks -------------------------------------------------- #

    def reset_message_input(self, turn_state: InterviewerTurnState) -> None:
        """Reset the input state after a worker finishes."""

        if self.active_turn_state is not turn_state:
            return
        self.active_turn_state = None
        self.last_escape_time = None
        self.is_interviewer_generating = False
        self.set_focus(self.message_input)

    async def render_chat_event(self, turn_state: InterviewerTurnState, chat_event: ChatEvent) -> None:
        """Render one streamed event into the current turn."""

        if turn_state.is_halted:
            return
        if turn_state.placeholder is not None:
            await turn_state.placeholder.remove()
            turn_state.placeholder = None

        match chat_event:
            case ReasoningDelta(text=text):
                await self.append_reasoning_text(turn_state, text)
            case TextDelta(text=text):
                turn_state.active_reasoning = None
                turn_state.active_reasoning_text = ""
                await self.append_interviewer_text(turn_state, text)
            case ToolCallStarted(call_id=call_id, label=label, symbol=symbol, depth=depth):
                turn_state.active_markdown = None
                turn_state.active_markdown_text = ""
                turn_state.active_reasoning = None
                turn_state.active_reasoning_text = ""
                turn_state.tool_rows[call_id] = ToolCallRow(label, symbol=symbol, depth=depth)
                await turn_state.container.mount(turn_state.tool_rows[call_id])
                self.messages_container.anchor()
            case ToolCallFinished(call_id=call_id, label=label, depth=depth):
                for nested_call_id, nested_row in list(turn_state.tool_rows.items()):
                    if nested_row.depth > depth:
                        await nested_row.remove()
                        del turn_state.tool_rows[nested_call_id]
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

    def action_halt_agent(self) -> None:
        """Stop the active agent loop after a second Escape press."""

        now = monotonic()
        if self.last_escape_time is None or now - self.last_escape_time > 1:
            self.last_escape_time = now
            self.notify("Press Esc again to stop", timeout=1)
            return

        self.last_escape_time = None
        turn_state = self.active_turn_state
        if turn_state is None:
            return
        interviewer = self.service.cancel()
        if interviewer is None:
            return
        turn_state.is_halted = True
        for tool_row in turn_state.tool_rows.values():
            if not tool_row.is_complete:
                tool_row.mark_complete(f"{tool_row.label} (stopped)")
        self.call_later(self.render_interviewer_status, turn_state, c.INTERVIEWER_HALTED_COPY)
        Thread(target=interviewer.close, name="cancel agent", daemon=True).start()
        self.reset_message_input(turn_state)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Enable agent halt only while a turn is active.

        Returns:
            Whether the action is currently available.
        """

        if action == "halt_agent":
            return self.is_interviewer_generating and not isinstance(self.screen, CommandPalette)
        return super().check_action(action, parameters)

    def action_toggle_keymap_panel(self) -> None:
        """Show or hide the keymap panel."""

        if self.screen.query("HelpPanel"):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def action_toggle_reasoning(self) -> None:
        """Show or hide reasoning summaries in this session."""

        self.is_reasoning_visible = not self.is_reasoning_visible
        self.service.set_show_thinking_blocks(show=self.is_reasoning_visible)
        for reasoning_block in self.query(Markdown):
            if reasoning_block.has_class(c.INTERVIEWER_REASONING_CLASSES):
                reasoning_block.display = self.is_reasoning_visible

    async def append_reasoning_text(self, turn_state: InterviewerTurnState, text: str) -> None:
        """Append streamed text to the active reasoning block."""

        if turn_state.active_reasoning is None:
            turn_state.active_markdown = None
            turn_state.active_markdown_text = ""
            turn_state.active_reasoning = Markdown("", classes=c.INTERVIEWER_REASONING_CLASSES)
            turn_state.active_reasoning.display = self.is_reasoning_visible
            await turn_state.container.mount(turn_state.active_reasoning)

        turn_state.active_reasoning_text += text
        await turn_state.active_reasoning.update(turn_state.active_reasoning_text)
        self.messages_container.anchor()

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

        items, self.is_reasoning_visible = self.service.restore()
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
            if item.type == "reasoning":
                reasoning_block = Markdown(item.text, classes=c.INTERVIEWER_REASONING_CLASSES)
                reasoning_block.display = self.is_reasoning_visible
                await interviewer_turn.mount(reasoning_block)
                continue
            await interviewer_turn.mount(
                ToolCallRow(item.text, symbol=item.symbol or "⚙︎", is_complete=True)
                if item.type == "tool"
                else Markdown(item.text, classes=c.INTERVIEWER_MESSAGE_CLASSES)
            )
        self.messages_container.scroll_end(animate=False)
