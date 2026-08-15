import logging
from collections.abc import Generator
from functools import cached_property
from threading import Event, Lock
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

from openai.types.responses import ResponseInputParam
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jri.lib import files, prompt

from .ai import (
    DEFAULT_SYMBOL,
    AgentEvent,
    Ending,
    Interviewer,
    Outcome,
    ReasoningDelta,
    TextDelta,
    Tool,
    ToolCallFinished,
    ToolCallStarted,
    TurnEvent,
    TurnFinished,
    specs_generation,
)
from .exceptions import (
    PersistenceError,
    ProviderRefusalError,
    ProviderUnavailableError,
    ReplayError,
    RepositoryStateError,
    UsageLimitError,
)
from .generation import Generation
from .notes import Graph, Notebook, TopicId
from .settings import Settings
from .workspace import Workspace

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputItemParam

# This records the last work of a turn. A retry performs this work again.
type Work = Literal["message", "generation", "reply"]
# This records how a turn ended. No `TurnFinished` event has the `interrupted` ending.
# This ending occurs when the holding process exits before it can end the turn. No component remains to yield the event.
type TurnEnding = Ending | Literal["interrupted"]

# These endings leave the opened work incomplete and can progress on retry.
# Keep this policy with the ending definition, not a view. A refusal is excluded because the provider repeats it.
# A retry would promise a second wait for the same answer.
RETRYABLE_ENDINGS = frozenset[TurnEnding]({"empty", "failed", "unavailable", "exhausted", "interrupted"})


class Item(BaseModel):
    type: Literal["assistant", "reasoning", "tool"]
    text: str = ""
    symbol: str = DEFAULT_SYMBOL
    # No outcome means the row was still open when the session was saved.
    outcome: Outcome | None = None
    detail: str = ""

    model_config = ConfigDict(extra="forbid")


class Turn(BaseModel):
    message: str
    items: list[Item]
    # A failed run and a failed message leave the interview in the same state.
    # The session does not store this distinction.
    # A turn declares its work. Restart an older session instead of retrying it without this record.
    work: Work
    # No ending means the turn was still open when the session was saved. Save a turn before it finishes.
    # This field records its current result, not its normal result.
    # A reply claim would make the next window wait for one.
    ending: TurnEnding | None = None
    detail: str = ""

    model_config = ConfigDict(extra="forbid")


class Checkpoint(NamedTuple):
    history_length: int
    graph: Graph
    active_topic_id: TopicId


class Session(BaseModel):
    active_topic_id: TopicId
    initial_graph: Graph
    interview: list[dict[str, Any]] = Field(default_factory=list)
    transcript: list[Turn] = Field(default_factory=list)
    failed_call_ids: list[str] = Field(default_factory=list)
    ready_graph: Graph | None = None
    # A run that reported no ambiguities built the project. JRI cannot change a built project yet.
    # A rewind does not undo that build, so this record outlives the turns that a rewind drops.
    generated_project: bool = False
    show_thinking_blocks: bool = False
    # No theme means the window follows the system appearance. A theme is the one the user selected.
    theme: str | None = None

    model_config = ConfigDict(extra="forbid")


class Conversation:
    def __init__(self, settings: Settings) -> None:
        self.workspace = Workspace.find()

        self.session_lock = Lock()
        self.settings = settings
        self.logger = logging.getLogger(__name__)
        self.logger.info("initialized root=%r", self.workspace.root)

    @cached_property
    def notebook(self) -> Notebook:
        return Notebook(self.workspace.notebook_file)

    @cached_property
    def session(self) -> Session:
        return Session(
            active_topic_id=self.notebook.initial_topic.id, initial_graph=self.notebook.graph.model_copy(deep=True)
        )

    @cached_property
    def interviewer(self) -> Interviewer:
        return Interviewer(self.settings, self.notebook)

    @property
    def is_ready_to_ralph(self) -> bool:
        # The next note ID is an allocator, not content. A restored notebook keeps its highest allocated ID.
        # A rolled-back turn does not release its note IDs.
        offer = self.session.ready_graph
        return offer is not None and offer == self.notebook.graph.model_copy(
            update={"next_note_id": offer.next_note_id}
        )

    @property
    def retried_work(self) -> Work:
        return self.session.transcript[-1].work

    # This states whether the conversation must settle a generation that it started.
    # A generation turn and an unfolded journal are the required facts.
    # The journal distinguishes a live run from one without a watcher.
    @property
    def pending_generation(self) -> bool:
        return (
            bool(self.session.transcript)
            and self.session.transcript[-1].work == "generation"
            and Generation(self.workspace).exists
        )

    def chat(self, message: str, cancelled: Event | None = None) -> Generator[TurnEvent]:
        self.logger.info("chat_started")
        self.logger.debug("chat_message message=%r", message)
        checkpoint = self._capture_checkpoint(len(self.interviewer.history))
        turn = Turn(message=message, items=[], work="message")
        self.session.transcript.append(turn)
        yield from self._report_turn(self.interviewer.send_message(message, cancelled), turn, checkpoint, cancelled)

    def retry(self, cancelled: Event | None = None, detached: Event | None = None) -> Generator[TurnEvent]:
        work = self.retried_work
        # A failed run sent no report or request to the interviewer. There is no message to send again.
        # Retry the run, as the button that starts a run would do.
        if work == "generation":
            yield from self.ralph(cancelled, detached)
            return
        # A generation report opens a turn like a prompt. Send the turn again from its opening item.
        # Truncating at the last prompt would remove the report and make the model answer for an unknown run.
        opening = max(
            index
            for index, item in enumerate(self.interviewer.history)
            if cast("dict[str, Any]", item).get("role") in {"user", "system"}
        )
        item = cast("dict[str, Any]", self.interviewer.history[opening])
        checkpoint = self._capture_checkpoint(opening)
        if work == "reply":
            self.interviewer.history = self.interviewer.history[: opening + 1]
            events = self.interviewer.respond(cancelled)
        else:
            self.interviewer.history = self.interviewer.history[:opening]
            events = self.interviewer.send_message(cast("str", item["content"]), cancelled)
        # The turn repeats its prior work.
        # Retry failures therefore repeat the work instead of sending an opening report as a message.
        turn = Turn(message=self.session.transcript[-1].message, items=[], work=work)
        self.session.transcript[-1] = turn
        yield from self._report_turn(events, turn, checkpoint, cancelled)

    def rewind(self, checkpoint_index: int) -> None:
        prompts = [
            index
            for index, item in enumerate(self.interviewer.history)
            if cast("dict[str, Any]", item).get("role") == "user"
        ]
        history_index = prompts[checkpoint_index]
        kept = [cast("dict[str, Any]", item) for item in self.interviewer.history[:history_index]]
        tools = {tool.name: tool for tool in self.interviewer.tools}
        # Replaying the calls below rebuilds the notes. A rewind depends on whether the replay creates the notes again.
        # Do not depend on whether JRI still has the tool name. Check this before any rollback.
        unreplayable = next(
            (
                item["name"]
                for item in kept
                if item.get("type") == "function_call"
                and item["call_id"] not in self.session.failed_call_ids
                and not _can_replay(tools.get(item["name"]), item["arguments"])
            ),
            None,
        )
        if unreplayable is not None:
            self.logger.info("rewind_refused checkpoint=%d tool=%s", checkpoint_index, unreplayable)
            raise PersistenceError(
                f"This conversation calls `{unreplayable}` in a way this version of JRI cannot make again, so "
                "the notes cannot be rebuilt as they were. Nothing changed: rewind to a message before that "
                "call, or keep going from here."
            )

        # Save all state that the replay will change.
        # A turn retires its offer, so save the graph stamped in the session.
        session = self.session
        history = self.interviewer.history
        graph = self.notebook.graph.model_copy(deep=True)
        active_topic_id = self.interviewer.active_topic_id

        self.interviewer.history = history[:history_index]
        # The kept calls are replayed below. They must find the note IDs that their original calls used.
        self.notebook.restore(self.session.initial_graph, reuse_note_ids=True)
        self.interviewer.active_topic_id = self.notebook.initial_topic.id
        self.session = self.session.model_copy(update={"ready_graph": None})
        self.interviewer.offered_ralphing = False

        try:
            self._replay(kept, tools)
        except Exception:
            self.session = session
            self.interviewer.history = history
            self.notebook.restore(graph)
            self.interviewer.active_topic_id = active_topic_id
            self.interviewer.offered_ralphing = False
            self.logger.info("rewind_failed checkpoint=%d", checkpoint_index)
            raise

        del self.session.transcript[checkpoint_index:]
        # A rewind restores notes from before the dropped turns. A draft based on those notes is no longer valid.
        # The next run writes from the specifications that the project holds.
        self.workspace.drop_draft()
        self._save_interview()
        self.logger.info("rewound checkpoint=%d interview_items=%d", checkpoint_index, history_index)

    def ralph(self, cancelled: Event | None = None, detached: Event | None = None) -> Generator[TurnEvent]:
        checkpoint = self._capture_checkpoint(len(self.interviewer.history))
        # A run reports into the open turn. Its rows and reply answer the message that opened that turn.
        if not self.session.transcript:
            self.session.transcript.append(Turn(message="", items=[], work="generation"))
        # The turn is open again. Its previous ending does not describe this run.
        opened = self.session.transcript[-1]
        opened.work = "generation"
        opened.ending = None
        opened.detail = ""
        # Save the turn before the run leaves this process. A new window can then identify the work to resume.
        self.update_session(transcript=self.session.transcript)
        # Keep the running record in its journal until it folds. The session holds the turn state from before the run.
        # A `^t` during the run cannot save a partial run.
        # Folding the same journal twice adds the same rows to its turn.
        # This prevents a window that exits during folding from adding rows to the prior fold.
        turn = opened.model_copy(deep=True)
        yield from self._report_turn(self._generate_specs(cancelled, detached, turn), turn, checkpoint, cancelled)

    def restore(self) -> list[Turn]:
        if not self.workspace.session_file.exists():
            self.logger.info("restore_skipped reason=no_session_file")
            return []
        try:
            self.session = Session.model_validate_json(self.workspace.session_file.read_bytes())
            # The session names the topic the interview was on. Look that topic up, and let the `LookupError` of a
            # notebook that no longer holds it report the session as unusable, beside every other unreadable part.
            topics = {topic.id: topic for topic in self.notebook.graph.topics if topic.status != "trashed"}
            topics[self.session.active_topic_id]
            history = self._read_interview()
        except (OSError, ValidationError, LookupError, TypeError) as error:
            raise PersistenceError(
                f"Invalid session file `{self.workspace.session_file}`. Delete it to start a new conversation, "
                "or run `jri init --force` to reset the whole workspace, notes included."
            ) from error
        self.interviewer.history = history
        self.interviewer.active_topic_id = self.session.active_topic_id
        self.interviewer.failed_call_ids = list(self.session.failed_call_ids)
        self.interviewer.generated_project = self.session.generated_project
        self.logger.info("restored interview_items=%d", len(self.session.interview))
        self._settle_interrupted_turn()
        return self.session.transcript

    def update_session(self, **values: object) -> None:
        with self.session_lock:
            session = self.session.model_copy(
                update={"failed_call_ids": list(self.interviewer.failed_call_ids), **values}
            )
            try:
                files.write_atomically(self.workspace.session_file, session.model_dump_json())
            except OSError as error:
                self.logger.exception("session_write_failed path=%r", self.workspace.session_file)
                raise PersistenceError(
                    f"Could not save the session file `{self.workspace.session_file}`: {error.strerror}"
                ) from error
            self.session = session
        self.logger.info("session_updated fields=%r interview_items=%d", list(values), len(self.session.interview))

    def _read_interview(self) -> ResponseInputParam:
        # A session saved before its first turn has no interview. A saved interview has an older system prompt.
        # The current system prompt replaces that prompt.
        if not self.session.interview:
            return self.interviewer.history
        return [self.interviewer.history[0], *cast("ResponseInputParam", self.session.interview[1:])]

    # An open turn in the session has no window. This window starts while one JRI holds the project.
    # Unless its run outlived the old window, no process can end the turn.
    # Record it as interrupted so the user can retry it.
    def _settle_interrupted_turn(self) -> None:
        if not self.session.transcript or self.session.transcript[-1].ending is not None:
            return
        # The journal is not the only record. A runner takes its lock before it writes the first journal line.
        # A window can exit during this import delay. The runner can still be working with an empty run directory.
        if self.pending_generation or Generation(self.workspace).is_running:
            return
        self.session.transcript[-1].ending = "interrupted"
        self.update_session(transcript=self.session.transcript)
        self.logger.info("turn_interrupted work=%s", self.session.transcript[-1].work)

    def _capture_checkpoint(self, history_length: int) -> Checkpoint:
        return Checkpoint(history_length, self.notebook.graph.model_copy(deep=True), self.interviewer.active_topic_id)

    def _replay(self, kept: list[dict[str, Any]], tools: dict[str, Tool]) -> None:
        for item in kept:
            # An offer belongs to the turn that created it. The next prompt retires any offer that the replay restored.
            if item.get("role") == "user":
                self.interviewer.offered_ralphing = False
            if item.get("type") != "function_call" or item["call_id"] in self.session.failed_call_ids:
                continue
            try:
                tools[item["name"]].replay(item["arguments"])
            except ReplayError as error:
                raise PersistenceError(
                    f"This conversation calls `{item['name']}`, and calling it again failed, so the notes "
                    "cannot be rebuilt as they were. Nothing changed: rewind to a message before that call, "
                    f"or keep going from here. The call reported: {error}"
                ) from error

    def _generate_specs(self, cancelled: Event | None, detached: Event | None, turn: Turn) -> Generator[AgentEvent]:
        # Use one entry point to start or resume a run. The turn reports what its journal records.
        # Events can arrive live or all at once after forty minutes.
        generation = Generation(self.workspace)
        if not generation.exists:
            generation.start()
        # A leaving window stops watching only. `RunDetached` is not a failure, so `_report_turn` does not end the turn.
        # The window that resumes the run ends the turn.
        result = yield from generation.follow(cancelled, detached)
        # A user-stopped run has no conclusion to report.
        # It consumed no offer, and the model receives no empty run report.
        if result is None:
            return

        # A history item is permanent. It states what happened, not what to do next.
        # The prompt owns actions that persist.
        if isinstance(result, str):
            self.interviewer.generated_project = True
            workflow_result = (
                "Specification generation found no ambiguities: the specifications it wrote from the notebook as "
                "it stands are the ones the project now holds, and they were committed."
            )
        elif isinstance(result, specs_generation.Unchanged):
            self.interviewer.generated_project = True
            workflow_result = (
                "Specification generation found no ambiguities and changed nothing: the specifications the project "
                "already holds are the ones the notebook asks for, so no commit was made."
            )
        else:
            workflow_result = prompt.render(specification_generation_ambiguities=result.ambiguities)
        report: ResponseInputItemParam = {"role": "system", "content": workflow_result}
        self.interviewer.history.append(report)
        # After the report, the run is complete.
        # A retry repeats the reply, not the run that the project already committed.
        # The same save moves run rows from the journal to the session. No row is absent from both records.
        turn.work = "reply"
        self.session.transcript[-1] = turn
        # A run that reports consumes its offered notes, regardless of its conclusion.
        # A run without a report leaves the offer active.
        self.update_session(
            interview=self.interviewer.history, ready_graph=None, generated_project=self.interviewer.generated_project
        )
        yield from self.interviewer.respond(cancelled)

    # This is the only place that saves and ends a turn. Both views read this record instead of deriving their own.
    # The restored conversation is therefore the conversation that the user saw.
    def _report_turn(
        self, events: Generator[AgentEvent], turn: Turn, checkpoint: Checkpoint, cancelled: Event | None
    ) -> Generator[TurnEvent]:
        start = len(turn.items)
        open_rows: list[tuple[ToolCallStarted, Item | None]] = []
        open_text: Item | None = None
        failure: Exception | None = None
        try:
            for event in events:
                open_text = _record_event(turn, open_text, open_rows, event)
                yield event
        except Exception as error:
            # Keep what the user already saw. Roll back changes made behind it.
            self._roll_back(checkpoint)
            self.logger.exception("turn_failed")
            failure = error
        finally:
            events.close()

        stopped = cancelled is not None and cancelled.is_set()
        replied = any(item.type == "assistant" for item in turn.items[start:])
        if isinstance(failure, UsageLimitError):
            ending: Ending = "exhausted"
        elif isinstance(failure, ProviderRefusalError):
            ending = "refused"
        elif isinstance(failure, ProviderUnavailableError):
            ending = "unavailable"
        elif isinstance(failure, RepositoryStateError):
            ending = "blocked"
        elif failure is not None:
            ending = "failed"
        elif replied:
            ending = "replied"
        elif stopped:
            ending = "stopped"
        else:
            ending = "empty"

        # Close each row that remains open when the turn ends with a real event.
        # The recording and renderer then use their existing completed-call path.
        for row, item in reversed(open_rows):
            closing = ToolCallFinished(row.call_id, row.label, "stopped" if stopped else "failed", depth=row.depth)
            if item is not None:
                _close_row(item, closing)
            yield closing
        turn.ending = ending
        turn.detail = str(failure) if failure is not None else ""
        # Replace the session turn with the separate recorded turn. The next launch then reads the turn that ended.
        self.session.transcript[-1] = turn
        self._save_interview()
        self.logger.info("turn_finished ending=%s interview_items=%d", ending, len(self.interviewer.history))
        yield TurnFinished(ending, turn.detail)

    # The item that opened a turn outlives the turn. The user can retry it.
    def _roll_back(self, checkpoint: Checkpoint) -> None:
        self.interviewer.history = self.interviewer.history[: checkpoint.history_length + 1]
        self.notebook.restore(checkpoint.graph)
        self.interviewer.active_topic_id = checkpoint.active_topic_id
        self.interviewer.offered_ralphing = False

    # Write down the interview where it now stands. A turn and a rewind both leave it at rest.
    # An offer is the notes that caused it. Stamp it with the notebook that the interview rests on.
    # Notes connected after the offer are part of the offer. A turn without an offer does not clear an earlier stamp.
    # The offer belongs to the turn that made it, so retire it with the same save.
    def _save_interview(self) -> None:
        offer: dict[str, Graph] = (
            {"ready_graph": self.notebook.graph.model_copy(deep=True)} if self.interviewer.offered_ralphing else {}
        )
        self.interviewer.offered_ralphing = False
        self.update_session(
            active_topic_id=self.interviewer.active_topic_id,
            interview=self.interviewer.history,
            transcript=self.session.transcript,
            **offer,
        )


# A non-replayed tool creates nothing again, regardless of its arguments. Its arguments cannot affect a note.
# A replayed tool must still accept the call.
# `invoke` renders invalid arguments for a model that a rewind does not have.
# JRI does not record which missing tool type it has. Treat either type as notes at stake.
def _can_replay(tool: Tool | None, arguments: str) -> bool:
    if tool is None:
        return False
    if not tool.replayed:
        return True
    try:
        tool.arguments_model.model_validate_json(arguments, strict=True)
    except ValidationError:
        return False
    return True


def _close_row(item: Item, event: ToolCallFinished) -> None:
    item.text = event.label
    item.outcome = event.outcome
    item.detail = event.detail


# This is the item that accepts current deltas, or `None` before a new item starts.
# A row opening ends prior text at every depth. A row closing ends no text.
# A tool call between thoughts creates two live blocks and two restored items.
# A nested row still creates this screen boundary.
def _record_event(
    turn: Turn, open_text: Item | None, open_rows: list[tuple[ToolCallStarted, Item | None]], event: AgentEvent
) -> Item | None:
    match event:
        case ToolCallStarted():
            # Save a row where it opens. Save a delta streamed under it after the row, as the screen shows it.
            item = Item(type="tool", text=event.label, symbol=event.symbol) if not event.depth else None
            if item is not None:
                turn.items.append(item)
            open_rows.append((event, item))
            return None
        case ToolCallFinished():
            for index, (row, item) in enumerate(open_rows):
                if row.call_id == event.call_id:
                    if item is not None:
                        _close_row(item, event)
                    # Every row opened after a row is nested under it. Closing that row closes all nested rows.
                    del open_rows[index:]
                    break
            return open_text
        case TextDelta():
            return _record_text(turn, open_text, "assistant", event.text)
        case ReasoningDelta():
            return _record_text(turn, open_text, "reasoning", event.text)


def _record_text(turn: Turn, open_text: Item | None, type_: Literal["assistant", "reasoning"], text: str) -> Item:
    if open_text is not None and open_text.type == type_:
        open_text.text += text
        return open_text
    item = Item(type=type_, text=text)
    turn.items.append(item)
    return item
