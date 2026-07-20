import logging
import platform
import subprocess
from collections.abc import Callable, Iterable
from time import monotonic
from typing import Any, ClassVar, override

from openai import OpenAIError
from textual import work
from textual.app import App as TextualApp
from textual.app import ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.command import CommandPalette as TextualCommandPalette
from textual.containers import Vertical
from textual.reactive import Reactive
from textual.screen import Screen
from textual.widgets import Header, Markdown, Static

from jri.core.agents import ChatEvent, ReasoningDelta, TextDelta, ToolCallFinished, ToolCallStarted
from jri.core.exceptions import AuthError
from jri.core.service import Service

from . import constants as c
from .states import InterviewerTurnState
from .widgets import MessageInput, MessagesContainer, ToolCallRow

logger = logging.getLogger(__name__)


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
        Binding("escape", "cancel_turn", "Cancel response", show=False),
        Binding("ctrl+t", "toggle_reasoning", "Show/hide thinking blocks", show=False, priority=True),
    ]
    TITLE = c.TITLE_COPY
    CSS = c.STYLESHEET
    theme = Reactive(c.THEME_DARK)

    # Methods order:
    # 1. Magic methods
    # 2. Misc overrides
    # 2. Event handlers
    # 3. Actions
    # 4. Workers
    # 5. Callbacks
    # 6. Rendering helpers
    # 7. Misc helpers
    # Order alphabetically within each section, except for section 1

    def __init__(self, service: Service) -> None:
        super().__init__()
        if platform.system() == "Darwin":
            result = subprocess.run(
                ["/usr/bin/defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True, check=False
            )
            self.theme = c.THEME_DARK if result.stdout.strip() == "Dark" else c.THEME_LIGHT
        self.service = service
        self.restored_items, self.is_reasoning_visible = service.restore()
        self.restored_item_index = len(self.restored_items)
        self.is_restoring_history = False
        self.active_turn_state: InterviewerTurnState | None = None
        self.current_turns: list[tuple[Markdown, Vertical]] = []
        self.last_escape_at = 0.0
        self.messages_container = MessagesContainer(self._stop_following_bottom, self._load_older_history)
        self.message_input = MessageInput(id_=c.MESSAGE_INPUT_ID, placeholder=c.MESSAGE_INPUT_INITIAL_PLACEHOLDER_COPY)

    @override
    def compose(self) -> ComposeResult:
        """Compose the terminal layout.

        Yields:
            The widgets that make up the terminal UI.
        """

        yield Header(show_clock=True)
        with self.messages_container:
            yield Static()
        yield self.message_input

    @override
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

    # --- Event handlers --------------------------------------------- #

    def on_message_input_history_requested(self, event: MessageInput.HistoryRequested) -> None:
        """Preview message history or cancel the active turn."""

        if event.direction == "previous":
            if self.active_turn_state is not None:
                self._request_cancellation()
                return
            event.message_input.select_previous()
            self._preview_history()
        elif self.active_turn_state is None:
            event.message_input.select_next()
            self._preview_history()

    async def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        """Send a submitted user message to the interviewer."""

        if self.active_turn_state is not None:
            logger.info("message_submission_ignored reason=turn_active")
            return

        user_message = event.value.strip()

        if not user_message:
            event.message_input.text = ""
            logger.info("message_submission_ignored reason=blank_message")
            return

        logger.info("message_submitted characters=%d", len(user_message))

        if event.history_index is not None:
            self.service.rewind(event.history_index)
            await self._remove_turns(event.history_index)
        event.message_input.remember(user_message)
        event.message_input.placeholder = c.MESSAGE_INPUT_PLACEHOLDER_COPY
        self.last_escape_at = 0.0

        user_message_widget = Markdown(user_message, classes=c.USER_MESSAGE_CLASSES)
        interviewer_turn = Vertical(classes=c.INTERVIEWER_TURN_CLASSES)
        placeholder = Markdown(c.INTERVIEWER_THINKING_COPY, classes=c.INTERVIEWER_MESSAGE_CLASSES)
        turn_state = InterviewerTurnState(container=interviewer_turn, placeholder=placeholder)
        self.active_turn_state = turn_state
        self.current_turns.append((user_message_widget, interviewer_turn))

        await self.messages_container.mount(user_message_widget)
        await self.messages_container.mount(interviewer_turn)
        await interviewer_turn.mount(placeholder)

        self.messages_container.anchor()
        self._send_message(user_message, turn_state)

    async def on_mount(self) -> None:
        """Restore history and initialize the app state."""

        await self._restore_history()
        self.set_focus(self.message_input)
        logger.info("mounted theme=%s", self.theme)

    # --- Actions ---------------------------------------------------- #

    def action_cancel_turn(self) -> None:
        """Cancel an active turn after two Escape presses."""

        if self.active_turn_state is None:
            return
        now = monotonic()
        if now - self.last_escape_at <= 1:
            self._request_cancellation()
            return
        self.last_escape_at = now
        self.notify("Press Esc again to stop", timeout=1)

    @override
    def action_command_palette(self) -> None:
        """Show the command palette."""

        if isinstance(self.screen, CommandPalette):
            self.screen.action_previous_command()
        elif self.use_command_palette:
            self.push_screen(CommandPalette(id="--command-palette"))

    def action_toggle_keymap_panel(self) -> None:
        """Show or hide the keymap panel."""

        if self.screen.query("HelpPanel"):
            self.action_hide_help_panel()
            logger.info("keymap_panel_toggled visible=False")
        else:
            self.action_show_help_panel()
            logger.info("keymap_panel_toggled visible=True")

    def action_toggle_reasoning(self) -> None:
        """Show or hide reasoning summaries in this session."""

        self.is_reasoning_visible = not self.is_reasoning_visible
        logger.info("reasoning_visibility_toggled visible=%r", self.is_reasoning_visible)
        self.service.update_session(show_thinking_blocks=self.is_reasoning_visible)
        for reasoning_block in self.query(Markdown):
            if reasoning_block.has_class(c.INTERVIEWER_REASONING_CLASSES):
                reasoning_block.display = self.is_reasoning_visible

    # --- Workers ---------------------------------------------------- #

    @work(thread=True)
    def _send_message(self, user_message: str, turn_state: InterviewerTurnState) -> None:
        """Stream interviewer events for a user message."""

        status_copy = c.INTERVIEWER_NO_RESPONSE_COPY
        chat_events = self.service.chat(user_message)
        try:
            for chat_event in chat_events:
                if turn_state.cancelled.is_set():
                    break
                if isinstance(chat_event, TextDelta) and chat_event.text:
                    status_copy = None
                self._call_from_thread(self._render_chat_event, turn_state, chat_event)
        except OpenAIError as error:
            logger.exception("interviewer_provider_failed")
            error_text = str(error).lower()
            status_copy = (
                c.LLM_USAGE_LIMIT_COPY
                if any(term in error_text for term in ("usage limit", "quota", "available balance", "out of budget"))
                else c.INTERVIEWER_ERROR_COPY.format(error=error)
            )
        except (AuthError, RuntimeError) as error:
            logger.exception("interviewer_worker_failed")
            status_copy = c.INTERVIEWER_ERROR_COPY.format(error=error)
        except Exception:
            logger.exception("interviewer_worker_failed_unexpectedly")
            status_copy = c.INTERNAL_ERROR_COPY
        finally:
            chat_events.close()
            if turn_state.cancelled.is_set():
                self._call_from_thread(self._cancel_active_turn, turn_state)
            elif status_copy is not None and self.active_turn_state is turn_state:
                self._call_from_thread(self._render_interviewer_status, turn_state, status_copy)
            if not turn_state.cancelled.is_set():
                self._call_from_thread(self._reset_message_input, turn_state)

    # --- Callbacks -------------------------------------------------- #

    async def _cancel_active_turn(self, turn_state: InterviewerTurnState) -> None:
        if self.active_turn_state is not turn_state:
            return
        checkpoint_index = len(self.current_turns) - 1
        self.service.rewind(checkpoint_index)
        await self._remove_turns(checkpoint_index)
        self.message_input.cancel_latest()
        self._reset_message_input(turn_state)
        self.messages_container.scroll_end(animate=False)
        logger.info("interviewer_turn_cancelled checkpoint=%d", checkpoint_index)

    def _finish_restoring_history(self, old_scroll_y: float, old_max_scroll_y: int) -> None:
        self.messages_container.scroll_to(
            y=old_scroll_y + self.messages_container.max_scroll_y - old_max_scroll_y, animate=False, immediate=True
        )
        self.is_restoring_history = False

    async def _load_older_history(self) -> None:
        """Prepend the next batch of restored conversation turns."""

        if self.is_restoring_history or self.restored_item_index == 0:
            return
        self.is_restoring_history = True
        end = self.restored_item_index
        start = end
        turns = 0
        while start > 0:
            start -= 1
            if self.restored_items[start].type == "user":
                turns += 1
                if turns == c.HISTORY_BATCH_SIZE:
                    break

        widgets: list[Markdown | Vertical] = []
        interviewer_items: list[Markdown | ToolCallRow] = []
        for item in self.restored_items[start:end]:
            if item.type == "user":
                if interviewer_items:
                    widgets.append(Vertical(*interviewer_items, classes=c.INTERVIEWER_TURN_CLASSES))
                    interviewer_items = []
                widgets.append(Markdown(item.text, classes=c.USER_MESSAGE_CLASSES))
                continue
            if item.type == "reasoning":
                reasoning_block = Markdown(item.text, classes=c.INTERVIEWER_REASONING_CLASSES)
                reasoning_block.display = self.is_reasoning_visible
                interviewer_items.append(reasoning_block)
                continue
            interviewer_items.append(
                ToolCallRow(item.text, symbol=item.symbol or "⚙︎", is_complete=True)
                if item.type == "tool"
                else Markdown(item.text, classes=c.INTERVIEWER_MESSAGE_CLASSES)
            )
        if interviewer_items:
            widgets.append(Vertical(*interviewer_items, classes=c.INTERVIEWER_TURN_CLASSES))

        old_scroll_y = self.messages_container.scroll_y
        old_max_scroll_y = self.messages_container.max_scroll_y
        await self.messages_container.mount_all(widgets, before=1 if end < len(self.restored_items) else None)
        self.restored_item_index = start
        self.call_after_refresh(self._finish_restoring_history, old_scroll_y, old_max_scroll_y)

    async def _render_chat_event(self, turn_state: InterviewerTurnState, chat_event: ChatEvent) -> None:
        """Render one streamed event into the current turn."""

        if self.active_turn_state is not turn_state:
            logger.debug("chat_event_render_skipped type=%s", type(chat_event).__name__)
            return

        match chat_event:
            case ReasoningDelta():
                await self._render_reasoning_delta(turn_state, chat_event)
            case TextDelta():
                await self._render_text_delta(turn_state, chat_event)
            case ToolCallStarted():
                await self._render_tool_call_started(turn_state, chat_event)
            case ToolCallFinished():
                await self._render_tool_call_finished(turn_state, chat_event)
        self._follow_bottom(turn_state)

    async def _render_interviewer_status(self, turn_state: InterviewerTurnState, content: str) -> None:
        """Render a status message for the interviewer turn."""

        if turn_state.placeholder is None:
            turn_state.active_markdown = None
            turn_state.active_markdown_text = ""
            await turn_state.container.mount(Markdown(content, classes=c.INTERVIEWER_MESSAGE_CLASSES))
        else:
            await turn_state.placeholder.update(content)
        self._follow_bottom(turn_state)

    def _reset_message_input(self, turn_state: InterviewerTurnState) -> None:
        """Reset the input state after a worker finishes."""

        if self.active_turn_state is not turn_state:
            return
        self.active_turn_state = None
        self.set_focus(self.message_input)
        logger.debug("message_input_reset")

    def _stop_following_bottom(self) -> None:
        """Stop following streamed content after the user scrolls."""

        if self.active_turn_state is not None:
            self.active_turn_state.follow_bottom = False

    # --- Rendering helpers ----------------------------------------- #

    async def _render_reasoning_delta(self, turn_state: InterviewerTurnState, event: ReasoningDelta) -> None:
        if turn_state.active_reasoning is None:
            turn_state.active_markdown, turn_state.active_markdown_text = None, ""
            turn_state.active_reasoning = Markdown("", classes=c.INTERVIEWER_REASONING_CLASSES)
            turn_state.active_reasoning.display = self.is_reasoning_visible
            await turn_state.container.mount(turn_state.active_reasoning)

        turn_state.active_reasoning_text += event.text
        await turn_state.active_reasoning.update(turn_state.active_reasoning_text)

    @staticmethod
    async def _render_text_delta(turn_state: InterviewerTurnState, event: TextDelta) -> None:
        if turn_state.placeholder is not None:
            await turn_state.placeholder.remove()
            turn_state.placeholder = None
        turn_state.active_reasoning, turn_state.active_reasoning_text = None, ""
        if turn_state.active_markdown is None:
            turn_state.active_markdown = Markdown("", classes=c.INTERVIEWER_MESSAGE_CLASSES)
            await turn_state.container.mount(turn_state.active_markdown)

        turn_state.active_markdown_text += event.text
        await turn_state.active_markdown.update(turn_state.active_markdown_text)

    @staticmethod
    async def _render_tool_call_finished(turn_state: InterviewerTurnState, event: ToolCallFinished) -> None:
        for nested_call_id, nested_row in list(turn_state.tool_rows.items()):
            if nested_row.depth > event.depth:
                await nested_row.remove()
                del turn_state.tool_rows[nested_call_id]
        turn_state.tool_rows[event.call_id].mark_complete(event.label)
        if event.depth == 0:
            turn_state.placeholder = Markdown(c.INTERVIEWER_THINKING_COPY, classes=c.INTERVIEWER_MESSAGE_CLASSES)
            await turn_state.container.mount(turn_state.placeholder)

    @staticmethod
    async def _render_tool_call_started(turn_state: InterviewerTurnState, event: ToolCallStarted) -> None:
        if turn_state.placeholder is not None:
            await turn_state.placeholder.remove()
            turn_state.placeholder = None
        turn_state.active_markdown, turn_state.active_markdown_text = None, ""
        turn_state.active_reasoning, turn_state.active_reasoning_text = None, ""
        turn_state.tool_rows[event.call_id] = ToolCallRow(event.label, symbol=event.symbol, depth=event.depth)
        await turn_state.container.mount(turn_state.tool_rows[event.call_id])

    # --- Helpers ---------------------------------------------------- #

    def _call_from_thread(self, callback: Callable[..., Any], *args: object) -> None:
        if not self.is_running:
            return
        try:
            self.call_from_thread(callback, *args)
        except RuntimeError:
            if self.is_running:
                raise

    def _follow_bottom(self, turn_state: InterviewerTurnState) -> None:
        """Follow a generating turn to the bottom when enabled."""

        if turn_state.follow_bottom:
            self.messages_container.anchor()

    def _preview_history(self) -> None:
        for index, (user_message, interviewer_turn) in enumerate(self.current_turns):
            user_message.display = interviewer_turn.display = index < self.message_input.history_index
        self.messages_container.scroll_end(animate=False)

    async def _remove_turns(self, start: int) -> None:
        for user_message, interviewer_turn in self.current_turns[start:]:
            await user_message.remove()
            await interviewer_turn.remove()
        del self.current_turns[start:]

    def _request_cancellation(self) -> None:
        if self.active_turn_state is not None and not self.active_turn_state.cancelled.is_set():
            self.active_turn_state.cancelled.set()
            self.notify("Stopping response…", timeout=1)
            logger.info("interviewer_turn_cancellation_requested")

    async def _restore_history(self) -> None:
        """Rebuild the visible chat history from persisted items."""

        if self.restored_items:
            self.message_input.placeholder = c.MESSAGE_INPUT_PLACEHOLDER_COPY
            await self._load_older_history()
        logger.info(
            "history_restored items=%d remaining=%d reasoning_visible=%r",
            len(self.restored_items) - self.restored_item_index,
            self.restored_item_index,
            self.is_reasoning_visible,
        )
