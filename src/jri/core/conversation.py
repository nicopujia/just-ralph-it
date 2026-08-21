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
    prompts,
    specs_generation,
)
from .exceptions import (
    PersistenceError,
    ProviderRefusalError,
    ProviderUnavailableError,
    ReplayError,
    RepositoryStateError,
    RunStopped,
    UsageLimitError,
)
from .generation import Generation
from .notes import Graph, Notebook, TopicId
from .settings import Settings
from .workspace import Workspace

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputItemParam

# This records the last work of a turn. A retry does this work again.
type Work = Literal["message", "generation", "reply"]
# This records how a turn ended. No `TurnFinished` event has the `interrupted` ending.
# JRI writes this ending when the process that holds the turn exits before it can end the turn.
# No component is left to yield the event.
type TurnEnding = Ending | Literal["interrupted"]

# These endings leave the work incomplete, and a retry can continue that work.
# Keep this rule with the ending definition, and not in a view.
# This set holds no refusal, because the provider gives the same refusal again.
# A retry would only make the user wait a second time for the same answer.
RETRYABLE_ENDINGS = frozenset[TurnEnding]({"empty", "failed", "unavailable", "exhausted", "interrupted"})


class Item(BaseModel):
    type: Literal["assistant", "reasoning", "tool"]
    text: str = ""
    symbol: str = DEFAULT_SYMBOL
    # No outcome means that the row was still open when JRI saved the session.
    outcome: Outcome | None = None
    detail: str = ""

    model_config = ConfigDict(extra="forbid")


class Turn(BaseModel):
    message: str
    items: list[Item]
    # A failed run and a failed message leave the interview in the same state.
    # The session does not record which of the two failed.
    # A turn declares its work. Restart an older session that has no such record, and do not retry it.
    work: Work
    # No ending means that the turn was still open when JRI saved the session. JRI saves a turn before it ends.
    # This field records the current result of the turn, and not its usual result.
    # A recorded reply would make the next window wait for a reply that never comes.
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
    # The summary that replaces each recorded exploration report when the interview becomes too large. Without
    # this record, a restart keeps each saved report whole, because nothing can replace it again.
    output_summaries: dict[str, str] = Field(default_factory=dict)
    ready_graph: Graph | None = None
    # A run that reported no ambiguities built the project. JRI cannot change a built project yet.
    # A rewind does not undo that build, so this record outlives the turns that a rewind drops.
    generated_project: bool = False
    show_thinking_blocks: bool = False
    # No theme means that the window follows the system appearance. A theme is the theme that the user selected.
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
        return Notebook(self.workspace.notebook_file, self.workspace.root.name)

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

    # This states whether the conversation must settle a run.
    # The journal that no window removed is the only fact it needs.
    # A run that a window did not start writes that journal and nothing else, so the transcript cannot report it.
    # The window that finds the journal follows the run, whichever process asked for that run.
    @property
    def pending_generation(self) -> bool:
        return Generation(self.workspace).exists

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
        # A cut at the last prompt would remove the report, and the model would then answer for an unknown run.
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
        # The turn repeats the work it did before.
        # A retry that fails repeats that work, and does not send an opening report as a message.
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
        # The calls below rebuild the notes when JRI replays them.
        # A rewind depends on whether that replay makes the notes again, and not on whether JRI still has the
        # tool name. Check this before JRI rolls anything back.
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

        # Save every value that the replay changes.
        # A turn retires its offer, so save the graph that the session records.
        session = self.session
        history = self.interviewer.history
        graph = self.notebook.graph.model_copy(deep=True)
        active_topic_id = self.interviewer.active_topic_id

        self.interviewer.history = history[:history_index]
        # JRI replays the kept calls below. They must find the note IDs that their original calls used.
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
        # A rewind restores the notes from before the dropped turns.
        # A draft that JRI made from those notes is no longer valid.
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
        # Keep the record of the run in its journal until a window reads that journal into the turn.
        # The session holds the turn state from before the run, so a `^t` during the run cannot save a partial run.
        # A second read of the same journal would add the same rows to its turn again.
        # JRI thus works on a copy of the turn. A window that exits during the read then adds no row to the turn
        # that the session holds.
        turn = opened.model_copy(deep=True)
        yield from self._report_turn(self._generate_specs(cancelled, detached, turn), turn, checkpoint, cancelled)

    def restore(self) -> list[Turn]:
        if not self.workspace.session_file.exists():
            self.logger.info("restore_skipped reason=no_session_file")
            return []
        trashed = self.notebook.trashed_topic_ids
        try:
            self.session = Session.model_validate_json(self.workspace.session_file.read_bytes())
            # The session names the topic that the interview was on. Read that topic from the notebook.
            # A notebook that no longer holds the topic raises `LookupError`. That error reports the session as
            # unusable, with every other part that JRI cannot read.
            topics = {topic.id: topic for topic in self.notebook.graph.topics if topic.id not in trashed}
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
        self.interviewer.output_summaries = dict(self.session.output_summaries)
        self.interviewer.generated_project = self.session.generated_project
        self.logger.info("restored interview_items=%d", len(self.session.interview))
        self._settle_interrupted_turn()
        return self.session.transcript

    def update_session(self, **values: object) -> None:
        with self.session_lock:
            session = self.session.model_copy(
                update={
                    "failed_call_ids": list(self.interviewer.failed_call_ids),
                    "output_summaries": dict(self.interviewer.output_summaries),
                    **values,
                }
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
    # Only a run that outlived the old window can end that turn.
    # Record the turn as interrupted, so that the user can retry it.
    def _settle_interrupted_turn(self) -> None:
        if not self.session.transcript or self.session.transcript[-1].ending is not None:
            return
        # The journal is not the only record. A runner takes its lock before it writes the first journal line.
        # A window can exit during that import delay. The runner can still work while the run directory is empty.
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
            generation.spawn()
        # A window that closes stops its watch of the run, and it stops nothing else.
        # `RunDetached` is not a failure, so `_report_turn` does not end the turn.
        # The window that resumes the run ends the turn.
        result = yield from generation.follow(cancelled, detached)
        # A stopped run has no conclusion to report.
        # It consumed no offer, and the model receives no empty run report.
        # Report the stop, so that the turn ends on that stop, whichever process asked for it.
        if result is None:
            raise RunStopped

        # A history item is permanent. It states what happened, and not what to do next.
        # The prompt holds the actions that persist.
        # A commit and an unchanged project are one result to the interviewer, because the notes need no more work.
        # The interviewer cannot use the files that the run wrote, or the fact that the run committed them.
        if isinstance(result, str | specs_generation.Unchanged):
            self.interviewer.generated_project = True
            workflow_result = prompts.read("specs_generation_done")
        else:
            workflow_result = prompt.render(specs_generation_ambiguities=result.ambiguities)
        report: ResponseInputItemParam = {"role": "system", "content": workflow_result}
        self.interviewer.history.append(report)
        # The run is complete after the report.
        # A retry repeats the reply, and not the run that the project already committed.
        # The same save moves the run rows from the journal to the session, so one of the two records always
        # holds each row.
        turn.work = "reply"
        self.session.transcript[-1] = turn
        # A run that reports consumes its offered notes, regardless of its conclusion.
        # A run without a report leaves the offer active.
        self.update_session(
            interview=self.interviewer.history, ready_graph=None, generated_project=self.interviewer.generated_project
        )
        yield from self.interviewer.respond(cancelled)

    # This is the only place that saves and ends a turn. Both views read this record, and they make no record
    # of their own.
    # The restored conversation is the conversation that the user saw.
    def _report_turn(
        self, events: Generator[AgentEvent], turn: Turn, checkpoint: Checkpoint, cancelled: Event | None
    ) -> Generator[TurnEvent]:
        start = len(turn.items)
        open_rows: list[tuple[ToolCallStarted, Item | None]] = []
        open_text: Item | None = None
        failure: Exception | None = None
        # The run reports the stop that it read. A stop from outside this window never sets the event of this window.
        stopped = False
        try:
            for event in events:
                open_text = _record_event(turn, open_text, open_rows, event)
                yield event
        # A stop is not a failure. It keeps the checkpoint and ends the turn on the stop.
        except RunStopped:
            stopped = True
        except Exception as error:
            # Keep what the user already saw, and roll back the changes that the user did not see.
            self._roll_back(checkpoint)
            self.logger.exception("turn_failed")
            failure = error
        finally:
            events.close()

        # A turn that starts no run reads a stop from the event of this window only.
        stopped = stopped or (cancelled is not None and cancelled.is_set())
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

        # Close each row that is still open when the turn ends, and close it with a real event.
        # The recording and the renderer then use the path that they already have for a completed call.
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

    # Write the interview as it is now. A turn and a rewind both leave the interview complete.
    # An offer is the notes that caused it, so record the offer with the notebook that the interview holds.
    # The notes that a user connects after the offer are part of the offer.
    # A turn without an offer does not clear an earlier record.
    # The offer belongs to the turn that made it, so retire the offer with the same save.
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


# A tool that JRI does not replay makes nothing again, whatever its arguments are.
# Its arguments cannot change a note. A tool that JRI replays must still accept the call.
# Invalid arguments make `invoke` write a message for a model to read. A rewind has no model that reads it.
# JRI does not record which type a missing tool has, so treat both types as a risk to the notes.
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


# This is the item that accepts the current deltas, or `None` before a new item starts.
# A row that opens ends the text before it, at every depth. A row that closes ends no text.
# A tool call between two thoughts makes two live blocks and two restored items.
# A nested row also makes this boundary on the screen.
def _record_event(
    turn: Turn, open_text: Item | None, open_rows: list[tuple[ToolCallStarted, Item | None]], event: AgentEvent
) -> Item | None:
    match event:
        case ToolCallStarted():
            # Save a row at the place where it opens.
            # Save a delta that streams under that row after the row, because the screen shows them in that order.
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
                    # Every row that opens after a row is nested under it. JRI closes the nested rows with it.
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
