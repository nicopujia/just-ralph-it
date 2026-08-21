import logging
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass, field
from threading import Event
from time import monotonic
from typing import Any, ClassVar, cast, override

from textual import work
from textual.app import App as TextualApp
from textual.app import ComposeResult, SystemCommand
from textual.binding import ActiveBinding, Binding, BindingType
from textual.command import CommandPalette as TextualCommandPalette
from textual.containers import Container, Horizontal, Vertical
from textual.content import Content
from textual.reactive import Reactive
from textual.screen import Screen as TextualScreen
from textual.theme import Theme
from textual.widgets import Button, Footer, Header, HelpPanel, KeyPanel, LoadingIndicator, Markdown, Static

from jri.core.ai import (
    AgentEvent,
    ReasoningDelta,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TurnEvent,
    TurnFinished,
)
from jri.core.conversation import RETRYABLE_ENDINGS, Conversation, TurnEnding
from jri.core.exceptions import PersistenceError, RunDetached
from jri.lib import appearance

from . import copy, styles
from .widgets import (
    MessageInput,
    MessagesContainer,
    RunCancellationAnswer,
    RunCancellationDialog,
    ThinkingLabel,
    ToolCallRow,
)

logger = logging.getLogger(__name__)


@dataclass
class InterviewerTurnState:
    container: Vertical
    thinking_label: ThinkingLabel | None = None
    active_markdown: Markdown | None = None
    active_markdown_text: str = ""
    active_reasoning: Markdown | None = None
    active_reasoning_text: str = ""
    tool_rows: dict[str, ToolCallRow] = field(default_factory=dict)
    follow_bottom: bool = True
    is_ralphing: bool = False
    cancelled: Event = field(default_factory=Event)

    # A turn shows one thinking label at a time. This field holds it. A label that this field forgets stays on
    # the screen until the window closes. Thus mount and remove the label only here. A new label first removes
    # the label before it.
    async def hide_thinking_label(self) -> None:
        label, self.thinking_label = self.thinking_label, None
        if label is not None:
            await label.remove()

    async def show_thinking_label(self) -> None:
        await self.hide_thinking_label()
        self.thinking_label = ThinkingLabel(is_stopping=self.cancelled.is_set())
        await self.container.mount(self.thinking_label)


# This is a message the user sent while a turn was active. The turn stops, then this message opens the next turn.
@dataclass
class PendingMessage:
    text: str
    history_index: int | None


class CommandPalette(TextualCommandPalette):
    BINDINGS: ClassVar[list[BindingType]] = [
        *TextualCommandPalette.BINDINGS,
        Binding("ctrl+n", "cursor_down", copy.NEXT_COMMAND, show=False),
    ]

    def action_previous_command(self) -> None:
        self._action_command_list("cursor_up")


class Screen(TextualScreen[None]):
    # The keymap panel takes the keys, because a reader scrolls a long list. Textual builds that list from the widget
    # that has the keys, thus the panel would list only its own scroll keys. Read the list from the message input,
    # which is the widget that the reader asks about. Only the list uses this. Key presses go to the panel.
    @property
    @override
    def active_bindings(self) -> dict[str, ActiveBinding]:
        focused = self.focused
        if not isinstance(focused, KeyPanel):
            return super().active_bindings
        self.set_reactive(TextualScreen.focused, self.query_one(MessageInput))
        try:
            return super().active_bindings
        finally:
            self.set_reactive(TextualScreen.focused, focused)

    # The message input owns the shortcuts. It handles them only while this screen is active.
    # A screen over this screen takes the keys. Close the mode here to avoid hints for unavailable keys.
    def on_screen_suspend(self) -> None:
        self.query_one(MessageInput).is_shortcuts_open = False


class App(TextualApp[None]):
    # The window opens on the message input, because the user came here to write. Typing needs no click or Tab.
    # The app bindings stay available, because they have priority over the text-area keys.
    AUTO_FOCUS = f"#{styles.MESSAGE_INPUT_ID}"
    # The footer shows only the three exits from the current view.
    # All other bindings have `show=False` and are in the keymap panel.
    # A permanent hint has little value. The footer is the only terminal line that remains reserved.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+k", "toggle_keymap_panel", copy.KEYMAP_PANEL, priority=True),
        # Declare this binding instead of using Textual's. Textual calls it "palette".
        # The footer shows it on the right. Keep it hidden from the key list.
        Binding("ctrl+p", "command_palette", copy.COMMAND_PALETTE, show=False, priority=True),
        Binding("ctrl+q", "confirm_quit", copy.QUIT, priority=True),
        Binding("escape", "cancel_turn", copy.CANCEL_TURN, key_display=copy.CANCEL_TURN_KEY, show=False),
        Binding("ctrl+t", "toggle_reasoning", copy.THINKING_BLOCKS, show=False, priority=True),
    ]
    # Esc, ^q, and Enter stop a reply, and each one asks first. The question stays open for this many seconds.
    # Its notice stays on the screen for the same time, thus both end together.
    CONFIRMATION_WINDOW = 1.0
    HISTORY_BATCH_SIZE = 15
    TITLE = copy.TITLE
    CSS = styles.STYLESHEET
    theme = Reactive(styles.THEME_DARK)
    active_turn_state: Reactive[InterviewerTurnState | None] = Reactive(None, repaint=False)

    # Method order:
    # 1. Magic methods
    # 2. Other overrides
    # 3. Event methods
    # 4. Action methods
    # 5. Worker methods
    # 6. Callback methods
    # 7. Render methods
    # 8. Other helper methods
    # Keep alphabetical order in each section, except section 1.

    def __init__(self, conversation: Conversation) -> None:
        super().__init__()
        self.conversation = conversation
        # The header paints after this method, thus it shows the project name from its first paint.
        self._sync_project_name()
        # Set this event when the window closes. It signals a run that continues after the window.
        self.detached = Event()
        self.restored_turns = conversation.restore()
        # A saved theme is the selection the user made in this project. The system appearance opens the first window.
        self.theme = conversation.session.theme or (
            styles.THEME_LIGHT if appearance.read() == "light" else styles.THEME_DARK
        )
        self.is_reasoning_visible = conversation.session.show_thinking_blocks
        # Restored turns mount newest first. This is the conversation index of the first mounted turn.
        self.restored_turn_index = len(self.restored_turns)
        self.is_restoring_history = False
        self.mounted_turns: list[tuple[Markdown, Vertical]] = []
        # This is the time of the last press of each key that asks before it stops a reply.
        self.confirmations: dict[str, float] = {}
        self.pending_message: PendingMessage | None = None
        self.messages_container = MessagesContainer(self._stop_following_bottom, self._load_older_history)
        self.message_input = MessageInput(
            (turn.message for turn in self.restored_turns),
            id_=styles.MESSAGE_INPUT_ID,
            placeholder=copy.MESSAGE_INPUT_INITIAL_PLACEHOLDER,
        )
        self.ralph_button = Button(copy.RALPH_BUTTON, classes=styles.RALPH_BUTTON_CLASSES, compact=True)
        self.ralphing = Horizontal(LoadingIndicator(), Static(copy.RALPHING), classes=styles.RALPHING_CLASSES)
        self.input_box = Container(self.message_input, self.ralphing, id=styles.INPUT_BOX_ID)
        self.shortcut_hints = Static(classes=styles.SHORTCUT_HINTS_CLASSES)
        self.footer = Footer()

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
        yield self.input_box
        yield self.shortcut_hints
        yield self.footer

    # Textual dims the second half of the title and joins with an em dash. The project name is the half that
    # the reader looks for, thus dim the app name and its separator instead.
    @override
    def format_title(self, title: str, sub_title: str) -> Content:
        return Content.assemble((f"{title}{copy.TITLE_SEPARATOR}", "dim"), Content(sub_title))

    @override
    def get_default_screen(self) -> Screen:
        # Textual uses this id for its default screen. This screen only adds a key list and a suspend event method.
        # Code that uses the first screen id still finds it.
        return Screen(id="_default")

    @override
    def get_system_commands(self, screen: TextualScreen[object]) -> Iterable[SystemCommand]:
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

    # --- Event methods ---------------------------------------------- #

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.is_busy:
            return
        if event.button.has_class(styles.RETRY_BUTTON_CLASSES):
            await self._retry(event.button)
        elif event.button.has_class(styles.RALPH_BUTTON_CLASSES):
            await self._start_ralphing()

    async def on_message_input_history_requested(self, event: MessageInput.HistoryRequested) -> None:
        if event.direction == "previous":
            if self.is_busy:
                await self._request_cancellation()
                return
            event.message_input.select_previous()
            if event.message_input.history_index < self.restored_turn_index:
                await self._load_older_history(reveal_hidden=False)
            self._preview_history()
        elif not self.is_busy:
            event.message_input.select_next()
            self._preview_history()

    async def on_message_input_ralph_requested(self) -> None:
        if self.ralph_button.is_mounted and self.ralph_button.display and not self.is_busy:
            await self._start_ralphing()

    async def on_message_input_retry_requested(self) -> None:
        retry_buttons = list(self.query(Button).filter(f".{styles.RETRY_BUTTON_CLASSES}"))
        if retry_buttons and not self.is_busy:
            await self._retry(retry_buttons[-1])

    async def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        user_message = event.value.strip()

        if user_message == copy.QUIT_COMMAND:
            logger.info("quit_requested source=message")
            # Ask the turn to stop, as the quit key does. A worker thread that continues to read holds the process
            # open after the window goes, and with it the hold on the project.
            await self._request_cancellation()
            await self.action_quit()
            return

        if not user_message:
            event.message_input.text = ""
            logger.info("message_submission_ignored reason=blank_message")
            return

        # A new message stops the active turn. `_finish_turn` sends the message when that turn is closed and saved.
        # The empty input shows the user that JRI accepted the message.
        # A run disables the message input behind its panel, thus a run keeps the turn to its end.
        if self.is_busy:
            # The second press stops the reply on the screen. Enter is the key that sends, and the user presses it
            # after each thought, thus a press while a reply comes in can be a slip.
            # Ask first, and keep the text: JRI does not hold the message yet.
            if not self._read_confirmation(copy.SEND_MESSAGE_CONFIRMATION):
                logger.info("message_submission_confirmation_requested")
                return
            self.pending_message = PendingMessage(user_message, event.history_index)
            # This message is held. A press that replaces it answers no question that this window asked.
            self.confirmations.clear()
            event.message_input.text = ""
            logger.info("message_held characters=%d", len(user_message))
            await self._request_cancellation()
            return

        await self._send_message(user_message, event.history_index)

    async def on_mount(self) -> None:
        self.watch(self.message_input, "is_shortcuts_open", self._sync_shortcut_hints)
        # This signal reports the themes the user selects. The theme this window opened with does not reach it,
        # so a window that follows the system appearance keeps following it.
        self.theme_changed_signal.subscribe(self, self._save_theme)
        await self._restore_history()
        # Resume a pending run through its normal start path. This renders its rows, reply, and ending as usual.
        # On the first read, finish a run that ended without a window.
        if self.conversation.pending_generation:
            await self._start_ralphing()
        await self._sync_ralph_button()
        logger.info("mounted theme=%s", self.theme)

    # When the window closes, set this event. It stops window updates for a run that continues.
    # The worker then returns instead of holding the terminal during the run.
    def on_unmount(self) -> None:
        self.detached.set()
        logger.info("unmounted")

    def watch_active_turn_state(self) -> None:
        self.message_input.is_turn_active = self.is_busy

    # --- Action methods --------------------------------------------- #

    async def action_cancel_turn(self) -> None:
        turn_state = self.active_turn_state
        if turn_state is None:
            return
        # A reply comes back in seconds, and a second key press is a sufficient answer to stop it.
        # A run takes much longer, thus its stop asks in a dialog that gives the cost.
        if turn_state.is_ralphing:
            self.push_screen(RunCancellationDialog(), self._answer_run_cancellation)
            return
        if not self._read_confirmation(copy.CANCEL_TURN_CONFIRMATION):
            return
        await self._request_cancellation()

    @override
    def action_command_palette(self) -> None:
        if isinstance(self.screen, CommandPalette):
            self.screen.action_previous_command()
        elif self.use_command_palette:
            self.push_screen(CommandPalette(id="--command-palette"))

    # A key press near the keys that send a message can be a slip, and a reply goes with the window that shows it.
    # Warn before that, then stop the reply and close on the second press. Do not wait for the worker.
    # A run outlives its window, as its panel states, so a quit during a run only closes the window.
    # The typed quit command stays immediate, because the user wrote it.
    async def action_confirm_quit(self) -> None:
        turn_state = self.active_turn_state
        if turn_state is not None and not turn_state.is_ralphing:
            if not self._read_confirmation(copy.QUIT_CONFIRMATION):
                return
            await self._request_cancellation()
        logger.info("quit_requested source=key")
        await self.action_quit()

    async def action_toggle_keymap_panel(self) -> None:
        if keymap_panel := self.screen.query(HelpPanel):
            await keymap_panel.remove()
            self.message_input.focus()
            logger.info("keymap_panel_toggled visible=False")
            return
        await self.screen.mount(HelpPanel())
        # The panel is a reading list, and it can be longer than the window. Give it the keys, thus arrows,
        # Page keys, Home, and End scroll it. Textual gives its key list no focus, so make this one take the keys.
        key_panel = self.screen.query_one(KeyPanel)
        key_panel.can_focus = True
        key_panel.focus()
        # The letters of the open shortcuts go to the panel now. Close the mode to avoid hints for unavailable keys.
        self.message_input.is_shortcuts_open = False
        logger.info("keymap_panel_toggled visible=True")

    def action_toggle_reasoning(self) -> None:
        self.is_reasoning_visible = not self.is_reasoning_visible
        logger.info("reasoning_visibility_toggled visible=%r", self.is_reasoning_visible)
        self.conversation.update_session(show_thinking_blocks=self.is_reasoning_visible)
        for reasoning_block in self.query(Markdown):
            if reasoning_block.has_class(styles.INTERVIEWER_REASONING_CLASSES):
                reasoning_block.display = self.is_reasoning_visible

    # --- Worker methods --------------------------------------------- #

    @work(thread=True)
    def _run_turn(self, events: Generator[TurnEvent], turn_state: InterviewerTurnState) -> None:
        # The turn ends with `TurnFinished` in all cases. This worker does not leave a row waiting for a missing event.
        finished = TurnFinished("failed", copy.INTERNAL_ERROR)
        detached = False
        try:
            for event in events:
                if isinstance(event, TurnFinished):
                    finished = event
                    continue
                self._call_from_thread(self._render_agent_event, turn_state, event)
        # The window closes, but the run continues. Do not render an ending, close the turn, or write data.
        # The run records itself. A later window ends the turn from the saved data.
        except RunDetached:
            detached = True
            logger.info("turn_detached")
        except Exception as error:
            logger.exception("turn_worker_failed")
            finished = TurnFinished("failed", str(error))
        finally:
            events.close()
            if not detached:
                self._call_from_thread(self._finish_turn, turn_state, finished)

    # --- Callback methods ------------------------------------------- #

    async def _answer_run_cancellation(self, answer: RunCancellationAnswer | None) -> None:
        if answer == "stop":
            await self._request_cancellation()

    def _finish_restoring_history(self, old_scroll_y: float, old_max_scroll_y: int) -> None:
        self.messages_container.scroll_to(
            y=old_scroll_y + self.messages_container.max_scroll_y - old_max_scroll_y, animate=False, immediate=True
        )
        self.is_restoring_history = False
        self._sync_retry_shortcut()

    async def _finish_turn(self, turn_state: InterviewerTurnState, event: TurnFinished) -> None:
        if self.active_turn_state is not turn_state:
            return
        content, classes = _describe_ending(event.ending, event.detail)
        if content:
            await self._render_interviewer_status(turn_state, content, classes)
        else:
            # A turn with no content removes its thinking notice. The saved record has no notice.
            # The restored view uses that record.
            await turn_state.hide_thinking_label()
        # Put the retry control below the failure that it answers. A later failure gets a control below its own
        # message.
        if event.ending in RETRYABLE_ENDINGS:
            await turn_state.container.mount(self._build_retry_button())
        if turn_state.is_ralphing:
            self.ralphing.display = False
            self.message_input.disabled = False
            # A disabled widget cannot hold the keys, thus the run took them. Give them back, as at the window start.
            # A reader of the keymap panel keeps them, because the panel scrolls with the keys.
            if not isinstance(self.focused, KeyPanel):
                self.message_input.focus()
            self._mark_run(is_active=False)
            # The run ended by itself. Close its stop question, which now has nothing to stop.
            if isinstance(self.screen, RunCancellationDialog):
                self.screen.dismiss("keep")
        self._follow_bottom(turn_state)
        # A turn that failed puts the notebook back as it was, and with it the project name.
        self._sync_project_name()
        self.active_turn_state = None
        App.ALLOW_SELECT = True
        await self._sync_ralph_button()
        self._sync_retry_shortcut()
        logger.info("turn_ending_rendered ending=%s", event.ending)
        # The turn is closed and the session holds it. The message that stopped the turn opens the next turn.
        if self.pending_message is not None:
            pending_message = self.pending_message
            self.pending_message = None
            await self._send_message(pending_message.text, pending_message.history_index)

    async def _load_older_history(self, *, reveal_hidden: bool = True) -> None:
        if self.is_restoring_history:
            return
        self.is_restoring_history = True
        old_scroll_y = self.messages_container.scroll_y
        old_max_scroll_y = self.messages_container.max_scroll_y
        # During scrolling, hidden turns are a prefix. During history preview, they are a suffix.
        # Turn 0 stays visible during preview, so no older turns are available.
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

    async def _render_agent_event(self, turn_state: InterviewerTurnState, agent_event: AgentEvent) -> None:
        if self.active_turn_state is not turn_state:
            logger.debug("agent_event_render_skipped type=%s", type(agent_event).__name__)
            return

        match agent_event:
            case ReasoningDelta():
                await self._render_reasoning_delta(turn_state, agent_event)
            case TextDelta():
                await self._render_text_delta(turn_state, agent_event)
            case ToolCallStarted():
                await self._render_tool_call_started(turn_state, agent_event)
            case ToolCallFinished():
                await self._render_tool_call_finished(turn_state, agent_event)
        # The interviewer can rename the project during a turn.
        self._sync_project_name()
        self._follow_bottom(turn_state)

    async def _render_interviewer_status(self, turn_state: InterviewerTurnState, content: str, classes: str) -> None:
        # The status takes the place of the thinking label, which shows a wait that is now over.
        await turn_state.hide_thinking_label()
        turn_state.active_markdown = None
        turn_state.active_markdown_text = ""
        await turn_state.container.mount(Markdown(content, classes=classes))
        self._follow_bottom(turn_state)

    def _save_theme(self, theme: Theme) -> None:
        self.conversation.update_session(theme=theme.name)
        logger.info("theme_saved theme=%s", theme.name)

    def _stop_following_bottom(self) -> None:
        if self.active_turn_state is not None:
            self.active_turn_state.follow_bottom = False

    # --- Render methods --------------------------------------------- #

    async def _render_reasoning_delta(self, turn_state: InterviewerTurnState, event: ReasoningDelta) -> None:
        if turn_state.active_reasoning is None:
            turn_state.active_markdown, turn_state.active_markdown_text = None, ""
            turn_state.active_reasoning = Markdown("", classes=styles.INTERVIEWER_REASONING_CLASSES)
            turn_state.active_reasoning.display = self.is_reasoning_visible
            # The label reports a wait that continues below the thought. Keep it last, under the new block.
            await turn_state.container.mount(turn_state.active_reasoning, before=turn_state.thinking_label)

        turn_state.active_reasoning_text += event.text
        await turn_state.active_reasoning.update(turn_state.active_reasoning_text)

    @staticmethod
    async def _render_text_delta(turn_state: InterviewerTurnState, event: TextDelta) -> None:
        await turn_state.hide_thinking_label()
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
        turn_state.tool_rows[event.call_id].mark_complete(event.label, event.outcome, event.detail)
        if event.depth == 0:
            await turn_state.show_thinking_label()

    @staticmethod
    async def _render_tool_call_started(turn_state: InterviewerTurnState, event: ToolCallStarted) -> None:
        await turn_state.hide_thinking_label()
        turn_state.active_markdown, turn_state.active_markdown_text = None, ""
        turn_state.active_reasoning, turn_state.active_reasoning_text = None, ""
        row = ToolCallRow(event.label, symbol=event.symbol, depth=event.depth)
        row.age_by(event.age)
        if turn_state.cancelled.is_set():
            row.mark_stopping()
        turn_state.tool_rows[event.call_id] = row
        await turn_state.container.mount(row)

    # --- Helper methods --------------------------------------------- #

    def _build_restored_turns(self, start: int, end: int) -> list[tuple[Markdown, Vertical]]:
        # A retry runs the last conversation turn again. Only that turn can contain the retry control.
        # Newest turns restore first. The last turn is in the first batch, and later batches contain older turns.
        retried_turn = None if self.mounted_turns else self.restored_turns[-1]
        restored_turns: list[tuple[Markdown, Vertical]] = []
        for turn in self.restored_turns[start:end]:
            interviewer_items: list[Button | Markdown | ToolCallRow] = []
            for item in turn.items:
                if item.type == "reasoning":
                    reasoning_block = Markdown(item.text, classes=styles.INTERVIEWER_REASONING_CLASSES)
                    reasoning_block.display = self.is_reasoning_visible
                    interviewer_items.append(reasoning_block)
                    continue
                interviewer_items.append(
                    ToolCallRow(
                        item.text, symbol=item.symbol, is_complete=True, outcome=item.outcome, detail=item.detail
                    )
                    if item.type == "tool"
                    else Markdown(item.text, classes=styles.INTERVIEWER_MESSAGE_CLASSES)
                )
            content, classes = _describe_ending(turn.ending, turn.detail)
            if content:
                interviewer_items.append(Markdown(content, classes=classes))
            if turn.ending in RETRYABLE_ENDINGS and turn is retried_turn:
                interviewer_items.append(self._build_retry_button())
            restored_turns.append((
                Markdown(turn.message, classes=styles.USER_MESSAGE_CLASSES),
                Vertical(*interviewer_items, classes=styles.INTERVIEWER_TURN_CLASSES),
            ))
        return restored_turns

    @staticmethod
    def _build_retry_button() -> Button:
        return Button(copy.RETRY, classes=styles.RETRY_BUTTON_CLASSES, compact=True)

    def _build_shortcut_hints(self) -> str:
        message_input = self.message_input
        hints = (
            (copy.CLOSE_SHORTCUTS_KEY, copy.CLOSE_SHORTCUTS, True),
            (copy.UNDO_MESSAGE_LETTER, copy.UNDO_MESSAGE, message_input.history_index > 0),
            (copy.REDO_MESSAGE_LETTER, copy.REDO_MESSAGE, message_input.history_index < message_input.message_count),
            (copy.RETRY_LETTER, copy.RETRY, message_input.is_retry_ready),
            (copy.RALPH_LETTER, copy.RALPH_BUTTON, message_input.is_ralph_ready),
        )
        return "  ".join(
            f"[b]{key}[/b] {label}" if is_available else f"[dim]{key} {label}[/dim]"
            for key, label, is_available in hints
        )

    def _call_from_thread(self, callback: Callable[..., Any], *arguments: object) -> None:
        if not self.is_running:
            return
        try:
            self.call_from_thread(callback, *arguments)
        except RuntimeError:
            if self.is_running:
                raise

    # A retry repeats the last turn operation. Remove all retry controls before each new run.
    # An earlier control could repeat new work, not the work of its failure.
    async def _clear_retry_buttons(self) -> None:
        for retry_button in self.query(f".{styles.RETRY_BUTTON_CLASSES}"):
            await retry_button.remove()
        self._sync_retry_shortcut()

    def _follow_bottom(self, turn_state: InterviewerTurnState) -> None:
        if turn_state.follow_bottom:
            self.messages_container.anchor()

    def _hide_older_history(self) -> None:
        for index, (user_message, interviewer_turn) in enumerate(self.mounted_turns):
            user_message.display = interviewer_turn.display = index >= len(self.mounted_turns) - self.HISTORY_BATCH_SIZE

    # This CSS class carries the run colors of the panel and the scrollbar. The stylesheet keeps them together.
    # Mark the conversation screen, not the screen on top, because the stop dialog can be over it.
    def _mark_run(self, *, is_active: bool) -> None:
        self.screen_stack[0].set_class(is_active, styles.RUN_ACTIVE_CLASSES)

    def _preview_history(self) -> None:
        if self.message_input.history_index == self.message_input.message_count:
            self._hide_older_history()
        else:
            for index, (user_message, interviewer_turn) in enumerate(self.mounted_turns, self.restored_turn_index):
                user_message.display = interviewer_turn.display = index < self.message_input.history_index
        self.messages_container.scroll_end(animate=False)

    # A key that stops a reply asks before it acts. Answer whether this press answers the question that the press
    # before it asked. Each other press asks that question again.
    def _read_confirmation(self, question: str) -> bool:
        now = monotonic()
        if now - self.confirmations.get(question, 0.0) <= self.CONFIRMATION_WINDOW:
            return True
        self.confirmations[question] = now
        self.notify(question, timeout=self.CONFIRMATION_WINDOW)
        return False

    async def _remove_turns(self, start: int) -> None:
        offset = max(0, start - self.restored_turn_index)
        for user_message, interviewer_turn in self.mounted_turns[offset:]:
            await user_message.remove()
            await interviewer_turn.remove()
        del self.mounted_turns[offset:]

    # A run can take time to stop, and a model call can take minutes. Show the stopping status at once.
    # Do not keep the original status until the run returns.
    async def _request_cancellation(self) -> None:
        turn_state = self.active_turn_state
        if turn_state is None or turn_state.cancelled.is_set():
            return
        turn_state.cancelled.set()
        for row in turn_state.tool_rows.values():
            row.mark_stopping()
        self.notify(copy.CANCEL_TURN_STARTED, timeout=1)
        logger.info("interviewer_turn_cancellation_requested")
        if turn_state.thinking_label is not None:
            turn_state.thinking_label.mark_stopping()

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
        # While a retry runs, offer only the stop action.
        await self._clear_retry_buttons()
        # A retry reports in the existing turn. Keep its previous content, as for another run from the Ralph button.
        is_ralphing = self.conversation.retried_work == "generation"
        if is_ralphing:
            self._show_ralphing()
        else:
            for child in list(container.children):
                await child.remove()
        turn_state = InterviewerTurnState(container=container, is_ralphing=is_ralphing)
        await turn_state.show_thinking_label()
        self.active_turn_state = turn_state
        self.confirmations.clear()
        App.ALLOW_SELECT = False
        self.messages_container.anchor()
        self._run_turn(self.conversation.retry(turn_state.cancelled, self.detached), turn_state)

    async def _send_message(self, user_message: str, history_index: int | None) -> None:
        logger.info("message_submitted characters=%d", len(user_message))
        self.ralph_button.display = False

        if history_index is not None:
            # If rewind is refused, do not send the message. Keep the current offer on screen.
            # Display the provider name as text, not as terminal markup.
            try:
                self.conversation.rewind(history_index)
            except PersistenceError as error:
                logger.info("message_submission_ignored reason=rewind_refused")
                self.notify(str(error), severity="error", markup=False)
                await self._sync_ralph_button()
                return
            # The rewind puts the notebook back as it was, and with it the project name.
            self._sync_project_name()
            await self._remove_turns(history_index)
            self.restored_turns = self.restored_turns[:history_index]
            self.restored_turn_index = min(self.restored_turn_index, history_index)
        await self._clear_retry_buttons()
        self.message_input.remember(user_message)
        self.message_input.placeholder = copy.MESSAGE_INPUT_PLACEHOLDER
        self.confirmations.clear()

        user_message_widget = Markdown(user_message, classes=styles.USER_MESSAGE_CLASSES)
        interviewer_turn = Vertical(classes=styles.INTERVIEWER_TURN_CLASSES)
        turn_state = InterviewerTurnState(container=interviewer_turn)
        self.active_turn_state = turn_state
        # Textual can hit-test a block after `update()` detaches it. It then accesses a missing parent.
        App.ALLOW_SELECT = False
        self.mounted_turns.append((user_message_widget, interviewer_turn))

        await self.messages_container.mount(user_message_widget)
        await self.messages_container.mount(interviewer_turn)
        await turn_state.show_thinking_label()

        self._hide_older_history()
        self.messages_container.anchor()
        self._run_turn(self.conversation.chat(user_message, turn_state.cancelled), turn_state)

    # The panel covers the message input instead of replacing it. The input continues to set the container size.
    def _show_ralphing(self) -> None:
        self.message_input.disabled = True
        self.ralphing.display = True
        self._mark_run(is_active=True)

    async def _start_ralphing(self) -> None:
        if self.is_busy or not self.mounted_turns:
            return
        self.ralph_button.display = False
        await self._clear_retry_buttons()
        self._show_ralphing()
        # A run can add many rows. Hide model reasoning until the reader asks to see it.
        # Do not show a hint in the rows. It could tell readers who show reasoning to hide it.
        if not self.is_reasoning_visible:
            self.notify(copy.RALPHING_THINKING_HINT)
        turn_state = InterviewerTurnState(container=self.mounted_turns[-1][1], is_ralphing=True)
        self.active_turn_state = turn_state
        App.ALLOW_SELECT = False
        self.messages_container.anchor()
        self._run_turn(self.conversation.ralph(turn_state.cancelled, self.detached), turn_state)

    def _sync_project_name(self) -> None:
        self.sub_title = self.conversation.notebook.initial_topic.name

    async def _sync_ralph_button(self) -> None:
        if self.ralph_button.is_mounted:
            await self.ralph_button.remove()
        self.message_input.is_ralph_ready = False
        if self.is_busy or not self.conversation.is_ready_to_ralph or not self.mounted_turns:
            return
        # A generation retry starts the same run as this button. Show one offer under the failure that names it.
        if self.query(f".{styles.RETRY_BUTTON_CLASSES}") and self.conversation.retried_work == "generation":
            return
        self.ralph_button = Button(copy.RALPH_BUTTON, classes=styles.RALPH_BUTTON_CLASSES, compact=True)
        await self.mounted_turns[-1][1].mount(self.ralph_button)
        self.message_input.is_ralph_ready = True

    def _sync_retry_shortcut(self) -> None:
        self.message_input.is_retry_ready = bool(self.query(f".{styles.RETRY_BUTTON_CLASSES}"))

    # Textual selects footer entries from each binding `show` value.
    # It cannot show keys that change with a mode. Use this bar while the mode is open.
    def _sync_shortcut_hints(self) -> None:
        is_open = self.message_input.is_shortcuts_open
        if is_open:
            self.shortcut_hints.update(self._build_shortcut_hints())
        self.shortcut_hints.display = is_open
        self.footer.display = not is_open


# Handle every ending here. The live and restored views then use the same result.
# An unhandled ending does not satisfy the return type.
def _describe_ending(ending: TurnEnding | None, detail: str) -> tuple[str, str]:
    match ending:
        # An open turn has no ending status. This window resumes its associated run.
        # A runner can hold its lock before it writes a journal. `on_mount` reads the journal and draws the turn open.
        # That turn stays without a window until the user asks again.
        case None:
            return "", styles.INTERVIEWER_MESSAGE_CLASSES
        case "replied":
            return "", styles.INTERVIEWER_MESSAGE_CLASSES
        case "empty":
            return copy.TURN_NO_RESPONSE, styles.INTERVIEWER_MESSAGE_CLASSES
        case "stopped":
            return copy.TURN_STOPPED, styles.INTERVIEWER_MESSAGE_CLASSES
        # A closed window is not a turn failure.
        case "interrupted":
            return copy.TURN_INTERRUPTED, styles.INTERVIEWER_MESSAGE_CLASSES
        # Report only failures that JRI can cause. Report a provider refusal or unavailable service where it occurs.
        # Do not report them in the issue tracker.
        case "failed":
            return copy.TURN_ERROR.format(error=detail), styles.INTERVIEWER_ERROR_CLASSES
        case "refused":
            return copy.TURN_REFUSED.format(error=detail), styles.INTERVIEWER_ERROR_CLASSES
        case "unavailable":
            return copy.TURN_UNAVAILABLE.format(error=detail), styles.INTERVIEWER_ERROR_CLASSES
        case "exhausted":
            return copy.TURN_EXHAUSTED, styles.INTERVIEWER_ERROR_CLASSES
        # A repository state that the user must fix is not a crash. It is not a reply either.
        case "blocked":
            return copy.TURN_BLOCKED.format(error=detail), styles.INTERVIEWER_BLOCKED_CLASSES
        # A notebook that JRI cannot carry is a JRI failure, so it takes the failure border. The user clears nothing.
        case "oversized":
            return copy.TURN_OVERSIZED.format(error=detail), styles.INTERVIEWER_ERROR_CLASSES
