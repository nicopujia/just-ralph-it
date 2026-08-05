import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from threading import Event
from time import monotonic
from typing import Any, ClassVar, cast, override

from openai import OpenAIError
from textual import work
from textual.app import App as TextualApp
from textual.app import ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.command import CommandPalette as TextualCommandPalette
from textual.containers import Horizontal, Vertical
from textual.reactive import Reactive
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, LoadingIndicator, Markdown, Static

from jri.core.ai import ChatEvent, ReasoningDelta, TextDelta, ToolCallFinished, ToolCallStarted
from jri.core.conversation import Conversation
from jri.core.exceptions import RepositoryStateError
from jri.lib import appearance
from jri.lib.providers import codex

from . import copy, styles
from .widgets import MessageInput, MessagesContainer, ToolCallRow

logger = logging.getLogger(__name__)


@dataclass
class InterviewerTurnState:
    container: Vertical
    placeholder: Markdown | None
    active_markdown: Markdown | None = None
    active_markdown_text: str = ""
    active_reasoning: Markdown | None = None
    active_reasoning_text: str = ""
    tool_rows: dict[str, ToolCallRow] = field(default_factory=dict)
    retry_button: Button | None = None
    follow_bottom: bool = True
    cancelled: Event = field(default_factory=Event)


class CommandPalette(TextualCommandPalette):
    BINDINGS: ClassVar[list[BindingType]] = [
        *TextualCommandPalette.BINDINGS,
        Binding("ctrl+n", "cursor_down", copy.NEXT_COMMAND, show=False),
    ]

    def action_previous_command(self) -> None:
        self._action_command_list("cursor_up")


class App(TextualApp[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+k", "toggle_keymap_panel", copy.KEYMAP_PANEL, priority=True),
        Binding("escape", "cancel_turn", copy.CANCEL_TURN, key_display=copy.CANCEL_TURN_KEY),
        Binding("ctrl+t", "toggle_reasoning", copy.THINKING_BLOCKS, priority=True),
    ]
    HISTORY_BATCH_SIZE = 15
    TITLE = copy.TITLE
    CSS = styles.STYLESHEET
    theme = Reactive(styles.THEME_DARK)
    active_turn_state: Reactive[InterviewerTurnState | None] = Reactive(None, repaint=False)

    # Methods order:
    # 1. Magic methods
    # 2. Misc overrides
    # 3. Event handlers
    # 4. Actions
    # 5. Workers
    # 6. Callbacks
    # 7. Rendering helpers
    # 8. Misc helpers
    # Order alphabetically within each section, except for section 1

    def __init__(self, conversation: Conversation) -> None:
        super().__init__()
        self.theme = styles.THEME_LIGHT if appearance.read() == "light" else styles.THEME_DARK
        self.conversation = conversation
        self.restored_turns = conversation.restore()
        self.is_reasoning_visible = conversation.session.show_thinking_blocks
        # Restored turns mount newest-first, so this is also the
        # conversation index of the first mounted turn.
        self.restored_turn_index = len(self.restored_turns)
        self.is_restoring_history = False
        self.mounted_turns: list[tuple[Markdown, Vertical]] = []
        self.last_escape_at = 0.0
        self.messages_container = MessagesContainer(self._stop_following_bottom, self._load_older_history)
        self.message_input = MessageInput(
            (turn.message for turn in self.restored_turns),
            id_=styles.MESSAGE_INPUT_ID,
            placeholder=copy.MESSAGE_INPUT_INITIAL_PLACEHOLDER,
        )
        self.ralph_button = Button(copy.RALPH_BUTTON, classes=styles.RALPH_BUTTON_CLASSES, compact=True)
        self.ralphing = Horizontal(LoadingIndicator(), Static(copy.RALPHING), classes=styles.RALPHING_CLASSES)

    @override
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "cancel_turn":
            return self.is_busy
        return super().check_action(action, parameters)

    @override
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with self.messages_container:
            yield Static()
        yield self.message_input
        yield self.ralphing
        yield Footer(show_command_palette=False)

    @override
    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        for command in super().get_system_commands(screen):
            if command.title != "Maximize":
                yield command
        yield SystemCommand(
            copy.HIDE_THINKING_BLOCKS if self.is_reasoning_visible else copy.SHOW_THINKING_BLOCKS,
            copy.THINKING_BLOCKS_COMMAND,
            self.action_toggle_reasoning,
        )

    @property
    def is_busy(self) -> bool:
        return self.active_turn_state is not None

    # --- Event handlers --------------------------------------------- #

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.is_busy:
            return
        if event.button.has_class(styles.RETRY_BUTTON_CLASSES):
            await self._retry(event.button)
        elif event.button.has_class(styles.RALPH_BUTTON_CLASSES):
            self._start_ralphing()

    async def on_message_input_history_requested(self, event: MessageInput.HistoryRequested) -> None:
        if event.direction == "previous":
            if self.is_busy:
                self._request_cancellation()
                return
            event.message_input.select_previous()
            if event.message_input.history_index < self.restored_turn_index:
                await self._load_older_history(reveal_hidden=False)
            self._preview_history()
        elif not self.is_busy:
            event.message_input.select_next()
            self._preview_history()

    def on_message_input_ralph_requested(self) -> None:
        if self.ralph_button.is_mounted and self.ralph_button.display and not self.is_busy:
            self._start_ralphing()

    async def on_message_input_retry_requested(self) -> None:
        retry_buttons = [
            button for button in self.query(Button) if button.has_class(styles.RETRY_BUTTON_CLASSES) and button.display
        ]
        if retry_buttons and not self.is_busy:
            await self._retry(retry_buttons[-1])

    async def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        if self.is_busy:
            logger.info("message_submission_ignored reason=turn_active")
            return

        user_message = event.value.strip()

        if not user_message:
            event.message_input.text = ""
            logger.info("message_submission_ignored reason=blank_message")
            return

        logger.info("message_submitted characters=%d", len(user_message))
        self.ralph_button.display = False

        if event.history_index is not None:
            self.conversation.rewind(event.history_index)
            await self._remove_turns(event.history_index)
            self.restored_turns = self.restored_turns[: event.history_index]
            self.restored_turn_index = min(self.restored_turn_index, event.history_index)
        for retry_button in self.query(f".{styles.RETRY_BUTTON_CLASSES}"):
            await retry_button.remove()
        self._sync_retry_shortcut()
        event.message_input.remember(user_message)
        event.message_input.placeholder = copy.MESSAGE_INPUT_PLACEHOLDER
        self.last_escape_at = 0.0

        user_message_widget = Markdown(user_message, classes=styles.USER_MESSAGE_CLASSES)
        interviewer_turn = Vertical(classes=styles.INTERVIEWER_TURN_CLASSES)
        placeholder = Markdown(copy.INTERVIEWER_THINKING, classes=styles.INTERVIEWER_MESSAGE_CLASSES)
        turn_state = InterviewerTurnState(container=interviewer_turn, placeholder=placeholder)
        self.active_turn_state = turn_state
        # Textual may hit-test a block after update() detaches it.
        # Disable selection to avoid dereferencing its missing parent.
        # In other words, this prevents a crash from a Textual bug.
        App.ALLOW_SELECT = False
        self.mounted_turns.append((user_message_widget, interviewer_turn))

        await self.messages_container.mount(user_message_widget)
        await self.messages_container.mount(interviewer_turn)
        await interviewer_turn.mount(placeholder)

        self._hide_older_history()
        self.messages_container.anchor()
        self._send_message(user_message, turn_state)

    async def on_mount(self) -> None:
        await self._restore_history()
        await self._sync_ralph_button()
        self.set_focus(self.message_input)
        logger.info("mounted theme=%s", self.theme)

    def watch_active_turn_state(self) -> None:
        self.message_input.is_turn_active = self.is_busy

    # --- Actions ---------------------------------------------------- #

    def action_cancel_turn(self) -> None:
        if not self.is_busy:
            return
        now = monotonic()
        if now - self.last_escape_at <= 1:
            self._request_cancellation()
            return
        self.last_escape_at = now
        self.notify(copy.CANCEL_TURN_CONFIRMATION, timeout=1)

    @override
    def action_command_palette(self) -> None:
        if isinstance(self.screen, CommandPalette):
            self.screen.action_previous_command()
        elif self.use_command_palette:
            self.push_screen(CommandPalette(id="--command-palette"))

    def action_toggle_keymap_panel(self) -> None:
        if self.screen.query("HelpPanel"):
            self.action_hide_help_panel()
            logger.info("keymap_panel_toggled visible=False")
        else:
            self.action_show_help_panel()
            logger.info("keymap_panel_toggled visible=True")

    def action_toggle_reasoning(self) -> None:
        self.is_reasoning_visible = not self.is_reasoning_visible
        logger.info("reasoning_visibility_toggled visible=%r", self.is_reasoning_visible)
        self.conversation.update_session(show_thinking_blocks=self.is_reasoning_visible)
        for reasoning_block in self.query(Markdown):
            if reasoning_block.has_class(styles.INTERVIEWER_REASONING_CLASSES):
                reasoning_block.display = self.is_reasoning_visible

    # --- Workers ---------------------------------------------------- #

    @work(thread=True)
    def _ralph(self, turn_state: InterviewerTurnState) -> None:
        events = self.conversation.ralph()
        error: Exception | None = None
        try:
            for event in events:
                self._call_from_thread(self._render_chat_event, turn_state, event)
        except Exception as caught:
            logger.exception("ralphing_failed")
            error = caught
        finally:
            events.close()
            self._call_from_thread(self._finish_ralphing, turn_state, error)

    @work(thread=True)
    def _send_message(self, user_message: str | None, turn_state: InterviewerTurnState) -> None:
        replied = False
        error_copy: str | None = None
        chat_events = (
            self.conversation.retry(turn_state.cancelled)
            if user_message is None
            else self.conversation.chat(user_message, turn_state.cancelled)
        )
        try:
            for chat_event in chat_events:
                if isinstance(chat_event, TextDelta) and chat_event.text:
                    replied = True
                self._call_from_thread(self._render_chat_event, turn_state, chat_event)
        except OpenAIError as error:
            logger.exception("interviewer_provider_failed")
            error_text = str(error).lower()
            error_copy = (
                copy.LLM_USAGE_LIMIT
                if any(term in error_text for term in ("usage limit", "quota", "available balance", "out of budget"))
                else copy.INTERVIEWER_ERROR.format(error=error)
            )
        except (codex.AuthError, RuntimeError) as error:
            logger.exception("interviewer_worker_failed")
            error_copy = copy.INTERVIEWER_ERROR.format(error=error)
        except Exception:
            logger.exception("interviewer_worker_failed_unexpectedly")
            error_copy = copy.INTERNAL_ERROR
        finally:
            chat_events.close()
            if turn_state.cancelled.is_set():
                self._call_from_thread(self._finish_cancelled_turn, turn_state)
            elif self.active_turn_state is turn_state:
                if error_copy is not None:
                    self.conversation.update_session(failed_turn_error=error_copy)
                    self._call_from_thread(self._finish_failed_turn, turn_state, error_copy)
                elif not replied:
                    self._call_from_thread(self._finish_empty_turn, turn_state)
            self._call_from_thread(self._reset_message_input, turn_state)

    # --- Callbacks -------------------------------------------------- #

    async def _finish_cancelled_turn(self, turn_state: InterviewerTurnState) -> None:
        if self.active_turn_state is not turn_state:
            return
        for call_id, row in list(turn_state.tool_rows.items()):
            if not row.is_complete:
                await row.remove()
                del turn_state.tool_rows[call_id]
        if not turn_state.active_markdown_text:
            await self._render_interviewer_status(turn_state, copy.INTERVIEWER_STOPPED)
        self.messages_container.scroll_end(animate=False)
        logger.info("interviewer_turn_cancelled")

    async def _finish_empty_turn(self, turn_state: InterviewerTurnState) -> None:
        await self._render_interviewer_status(turn_state, copy.INTERVIEWER_NO_RESPONSE)
        await self._show_retry_button(turn_state)

    async def _finish_failed_turn(self, turn_state: InterviewerTurnState, error_copy: str) -> None:
        await self._render_interviewer_status(turn_state, error_copy, styles.INTERVIEWER_ERROR_CLASSES)
        await self._show_retry_button(turn_state)

    async def _finish_ralphing(self, turn_state: InterviewerTurnState, error: Exception | None) -> None:
        if self.active_turn_state is not turn_state:
            return
        if error is not None:
            for row in turn_state.tool_rows.values():
                if not row.is_complete:
                    row.mark_complete(copy.RALPH_INTERRUPTED)
                    break
            # A repository the user has to sort out is not a crash.
            blocked = isinstance(error, RepositoryStateError)
            await turn_state.container.mount(
                Markdown(
                    (copy.RALPH_BLOCKED if blocked else copy.RALPH_ERROR).format(error=error),
                    classes=styles.INTERVIEWER_MESSAGE_CLASSES if blocked else styles.INTERVIEWER_ERROR_CLASSES,
                )
            )
        elif turn_state.placeholder is not None:
            await self._render_interviewer_status(turn_state, copy.INTERVIEWER_NO_RESPONSE)
        self.ralphing.display = False
        self.message_input.display = True
        self.message_input.disabled = False
        self.active_turn_state = None
        App.ALLOW_SELECT = True
        await self._sync_ralph_button()
        self.set_focus(self.message_input)

    def _finish_restoring_history(self, old_scroll_y: float, old_max_scroll_y: int) -> None:
        self.messages_container.scroll_to(
            y=old_scroll_y + self.messages_container.max_scroll_y - old_max_scroll_y, animate=False, immediate=True
        )
        self.is_restoring_history = False
        self._sync_retry_shortcut()

    async def _load_older_history(self, *, reveal_hidden: bool = True) -> None:
        if self.is_restoring_history:
            return
        self.is_restoring_history = True
        old_scroll_y = self.messages_container.scroll_y
        old_max_scroll_y = self.messages_container.max_scroll_y
        # Hidden turns are a prefix while scrolling, but a suffix
        # while previewing history, where turn 0 stays visible and
        # there is nothing older to reveal.
        first_visible_turn = next(
            (index for index, (user_message, _) in enumerate(self.mounted_turns) if user_message.display),
            len(self.mounted_turns),
        )
        if reveal_hidden and first_visible_turn:
            for user_message, interviewer_turn in self.mounted_turns[
                max(0, first_visible_turn - self.HISTORY_BATCH_SIZE) : first_visible_turn
            ]:
                user_message.display = interviewer_turn.display = True
            self.call_after_refresh(self._finish_restoring_history, old_scroll_y, old_max_scroll_y)
            return
        if self.restored_turn_index == 0:
            self.is_restoring_history = False
            return

        end = self.restored_turn_index
        start = max(0, end - self.HISTORY_BATCH_SIZE)
        restored_turns = self._build_restored_turns(start, end)
        widgets = [widget for turn in restored_turns for widget in turn]
        await self.messages_container.mount_all(widgets, before=1 if self.mounted_turns else None)
        self.mounted_turns[0:0] = restored_turns
        self.restored_turn_index = start
        self.call_after_refresh(self._finish_restoring_history, old_scroll_y, old_max_scroll_y)

    async def _render_chat_event(self, turn_state: InterviewerTurnState, chat_event: ChatEvent) -> None:
        if self.active_turn_state is not turn_state:
            logger.debug("chat_event_render_skipped type=%s", type(chat_event).__name__)
            return
        if turn_state.retry_button is not None:
            turn_state.retry_button.display = False

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

    async def _render_interviewer_status(
        self, turn_state: InterviewerTurnState, content: str, classes: str = styles.INTERVIEWER_MESSAGE_CLASSES
    ) -> None:
        if turn_state.placeholder is None:
            turn_state.active_markdown = None
            turn_state.active_markdown_text = ""
            await turn_state.container.mount(Markdown(content, classes=classes))
        else:
            turn_state.placeholder.set_classes(classes)
            await turn_state.placeholder.update(content)
        self._follow_bottom(turn_state)

    def _reset_message_input(self, turn_state: InterviewerTurnState) -> None:
        if self.active_turn_state is not turn_state:
            return
        self.active_turn_state = None
        App.ALLOW_SELECT = True
        self.set_focus(self.message_input)
        self._sync_retry_shortcut()
        self.run_worker(self._sync_ralph_button())
        logger.debug("message_input_reset")

    def _stop_following_bottom(self) -> None:
        if self.active_turn_state is not None:
            self.active_turn_state.follow_bottom = False

    # --- Rendering helpers ----------------------------------------- #

    async def _render_reasoning_delta(self, turn_state: InterviewerTurnState, event: ReasoningDelta) -> None:
        if turn_state.active_reasoning is None:
            turn_state.active_markdown, turn_state.active_markdown_text = None, ""
            turn_state.active_reasoning = Markdown("", classes=styles.INTERVIEWER_REASONING_CLASSES)
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
            turn_state.active_markdown = Markdown("", classes=styles.INTERVIEWER_MESSAGE_CLASSES)
            await turn_state.container.mount(turn_state.active_markdown)

        turn_state.active_markdown_text += event.text
        await turn_state.active_markdown.update(turn_state.active_markdown_text)

    @staticmethod
    async def _render_tool_call_finished(turn_state: InterviewerTurnState, event: ToolCallFinished) -> None:
        for nested_call_id, nested_row in list(turn_state.tool_rows.items()):
            if nested_row.depth > event.depth:
                await nested_row.remove()
                del turn_state.tool_rows[nested_call_id]
        turn_state.tool_rows[event.call_id].mark_complete(event.label, has_failed=event.failed)
        if event.depth == 0:
            turn_state.placeholder = Markdown(copy.INTERVIEWER_THINKING, classes=styles.INTERVIEWER_MESSAGE_CLASSES)
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

    async def _show_retry_button(self, turn_state: InterviewerTurnState) -> None:
        if turn_state.retry_button is None:
            turn_state.retry_button = self._build_retry_button()
            await turn_state.container.mount(turn_state.retry_button)
        turn_state.retry_button.display = True
        turn_state.retry_button.disabled = False

    # --- Helpers ---------------------------------------------------- #

    def _build_restored_turns(self, start: int, end: int) -> list[tuple[Markdown, Vertical]]:
        restored_turns: list[tuple[Markdown, Vertical]] = []
        for turn in self.restored_turns[start:end]:
            interviewer_items: list[Button | Markdown | ToolCallRow] = []
            failed = False
            for item in turn.items:
                if item.type == "reasoning":
                    reasoning_block = Markdown(item.text, classes=styles.INTERVIEWER_REASONING_CLASSES)
                    reasoning_block.display = self.is_reasoning_visible
                    interviewer_items.append(reasoning_block)
                    continue
                if item.type == "error":
                    failed = True
                    interviewer_items.append(Markdown(item.text, classes=styles.INTERVIEWER_ERROR_CLASSES))
                    continue
                if item.type == "stopped":
                    interviewer_items.append(
                        Markdown(copy.INTERVIEWER_STOPPED, classes=styles.INTERVIEWER_MESSAGE_CLASSES)
                    )
                    continue
                interviewer_items.append(
                    ToolCallRow(item.text, symbol=item.symbol, is_complete=True, has_failed=item.failed)
                    if item.type == "tool"
                    else Markdown(item.text, classes=styles.INTERVIEWER_MESSAGE_CLASSES)
                )
            if failed or not interviewer_items:
                interviewer_items.append(self._build_retry_button())
            restored_turns.append((
                Markdown(turn.message, classes=styles.USER_MESSAGE_CLASSES),
                Vertical(*interviewer_items, classes=styles.INTERVIEWER_TURN_CLASSES),
            ))
        return restored_turns

    @staticmethod
    def _build_retry_button() -> Button:
        return Button(copy.RETRY, classes=styles.RETRY_BUTTON_CLASSES, compact=True)

    def _call_from_thread(self, callback: Callable[..., Any], *arguments: object) -> None:
        if not self.is_running:
            return
        try:
            self.call_from_thread(callback, *arguments)
        except RuntimeError:
            if self.is_running:
                raise

    def _follow_bottom(self, turn_state: InterviewerTurnState) -> None:
        if turn_state.follow_bottom:
            self.messages_container.anchor()

    def _hide_older_history(self) -> None:
        for index, (user_message, interviewer_turn) in enumerate(self.mounted_turns):
            user_message.display = interviewer_turn.display = index >= len(self.mounted_turns) - self.HISTORY_BATCH_SIZE

    def _preview_history(self) -> None:
        if self.message_input.history_index == self.message_input.message_count:
            self._hide_older_history()
        else:
            for index, (user_message, interviewer_turn) in enumerate(self.mounted_turns, self.restored_turn_index):
                user_message.display = interviewer_turn.display = index < self.message_input.history_index
        self.messages_container.scroll_end(animate=False)

    async def _remove_turns(self, start: int) -> None:
        offset = max(0, start - self.restored_turn_index)
        for user_message, interviewer_turn in self.mounted_turns[offset:]:
            await user_message.remove()
            await interviewer_turn.remove()
        del self.mounted_turns[offset:]

    def _request_cancellation(self) -> None:
        if self.active_turn_state is not None and not self.active_turn_state.cancelled.is_set():
            self.active_turn_state.cancelled.set()
            self.notify(copy.CANCEL_TURN_STARTED, timeout=1)
            logger.info("interviewer_turn_cancellation_requested")

    async def _restore_history(self) -> None:
        if self.restored_turns:
            self.message_input.placeholder = copy.MESSAGE_INPUT_PLACEHOLDER
            await self._load_older_history()
        logger.info(
            "history_restored turns=%d remaining=%d reasoning_visible=%r",
            len(self.restored_turns) - self.restored_turn_index,
            self.restored_turn_index,
            self.is_reasoning_visible,
        )

    async def _retry(self, button: Button) -> None:
        container = cast("Vertical", button.parent)
        self.ralph_button.display = False
        for child in list(container.children):
            if child is not button:
                await child.remove()
        placeholder = Markdown(copy.INTERVIEWER_THINKING, classes=styles.INTERVIEWER_MESSAGE_CLASSES)
        await container.mount(placeholder, before=button)
        turn_state = InterviewerTurnState(container=container, placeholder=placeholder, retry_button=button)
        self.active_turn_state = turn_state
        button.disabled = True
        self.last_escape_at = 0.0
        App.ALLOW_SELECT = False
        self.messages_container.anchor()
        self._send_message(None, turn_state)

    def _start_ralphing(self) -> None:
        if self.is_busy or not self.mounted_turns:
            return
        self.ralph_button.display = False
        self.message_input.disabled = True
        self.message_input.display = False
        self.ralphing.display = True
        turn_state = InterviewerTurnState(container=self.mounted_turns[-1][1], placeholder=None)
        self.active_turn_state = turn_state
        App.ALLOW_SELECT = False
        self.messages_container.anchor()
        self._ralph(turn_state)

    async def _sync_ralph_button(self) -> None:
        if self.ralph_button.is_mounted:
            await self.ralph_button.remove()
        self.message_input.is_ralph_ready = False
        if self.is_busy or not self.conversation.session.ready_to_ralph or not self.mounted_turns:
            return
        self.ralph_button = Button(copy.RALPH_BUTTON, classes=styles.RALPH_BUTTON_CLASSES, compact=True)
        await self.mounted_turns[-1][1].mount(self.ralph_button)
        self.message_input.is_ralph_ready = True

    def _sync_retry_shortcut(self) -> None:
        self.message_input.is_retry_ready = any(
            button.display for button in self.query(f".{styles.RETRY_BUTTON_CLASSES}")
        )
