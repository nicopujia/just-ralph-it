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
    ToolCallFinished,
    ToolCallStarted,
    TurnEvent,
    TurnFinished,
    specs_generation,
)
from .exceptions import PersistenceError, RepositoryStateError, UsageLimitError
from .notes import Graph, Notebook, TopicId
from .settings import Settings
from .workspace import Workspace

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputItemParam

# What a turn was last doing, and so what asking for it again re-runs.
type Work = Literal["message", "generation", "reply"]


class Item(BaseModel):
    type: Literal["assistant", "reasoning", "tool"]
    text: str = ""
    symbol: str = DEFAULT_SYMBOL
    outcome: Outcome = "done"
    detail: str = ""

    model_config = ConfigDict(extra="forbid")


class Turn(BaseModel):
    message: str
    items: list[Item]
    # A run that failed and a message that failed leave the interview
    # in the same state, so nothing reads this back out of a session
    # that never wrote it. A turn states it, and a file from before it
    # is one to start again from rather than one to retry blindly.
    work: Work
    ending: Ending = "replied"
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
    show_thinking_blocks: bool = False

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
        # The next note ID is an allocator rather than content, and a
        # notebook restored to a checkpoint keeps the highest one it
        # reached, so ids a rolled back turn spent revoke nothing.
        offer = self.session.ready_graph
        return offer is not None and offer == self.notebook.graph.model_copy(
            update={"next_note_id": offer.next_note_id}
        )

    @property
    def retried_work(self) -> Work:
        return self.session.transcript[-1].work

    def chat(self, message: str, cancelled: Event | None = None) -> Generator[TurnEvent]:
        self.logger.info("chat_started")
        self.logger.debug("chat_message message=%r", message)
        checkpoint = self._capture_checkpoint(len(self.interviewer.history))
        self.session.transcript.append(Turn(message=message, items=[], work="message"))
        yield from self._report_turn(self.interviewer.send_message(message, cancelled), checkpoint, cancelled)

    def retry(self, cancelled: Event | None = None) -> Generator[TurnEvent]:
        work = self.retried_work
        # A run that failed reported nothing to the interview and asked
        # it for nothing, so there is no message to send again: the work
        # to redo is the run, exactly as the button that starts one
        # would redo it.
        if work == "generation":
            yield from self.ralph(cancelled)
            return
        # A generation report opens a turn exactly as a prompt does, so
        # the turn is sent again from whichever opened it. Truncating to
        # the last prompt would drop the report out of the history and
        # leave the model answering a run it never heard about.
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
        # The turn is doing again what it was doing, so a retry that
        # fails is asked for again the same way, rather than sending a
        # report the interview opened the turn with as a message.
        self.session.transcript[-1] = Turn(message=self.session.transcript[-1].message, items=[], work=work)
        yield from self._report_turn(events, checkpoint, cancelled)

    def rewind(self, checkpoint_index: int) -> None:
        history_index = self._find_prompts()[checkpoint_index]
        kept = [cast("dict[str, Any]", item) for item in self.interviewer.history[:history_index]]
        tools = {tool.name: tool for tool in self.interviewer.tools}
        # The notes are rebuilt by replaying the calls below, so a call
        # this JRI cannot make is a notebook it cannot rebuild. It says
        # so before rolling anything back, rather than half way through.
        missing = next(
            (
                item["name"]
                for item in kept
                if item.get("type") == "function_call"
                and item["call_id"] not in self.session.failed_call_ids
                and item["name"] not in tools
            ),
            None,
        )
        if missing is not None:
            self.logger.info("rewind_refused checkpoint=%d tool=%s", checkpoint_index, missing)
            raise PersistenceError(
                f"This conversation calls `{missing}`, which this version of JRI no longer has, so the notes "
                "cannot be rebuilt as they were. Nothing changed: rewind to a message before that call, or "
                "keep going from here."
            )

        self.interviewer.history = self.interviewer.history[:history_index]
        # The calls kept are replayed below, so they have to name their
        # notes exactly as the calls that referenced them expect.
        self.notebook.restore(self.session.initial_graph, reuse_note_ids=True)
        self.interviewer.active_topic_id = self.notebook.initial_topic.id
        self.session = self.session.model_copy(update={"ready_graph": None})
        self.interviewer.offered_ralphing = False

        for item in kept:
            # An offer belongs to the turn that made it, so the prompt
            # opening the next one retires whatever the replay re-made.
            if item.get("role") == "user":
                self.interviewer.offered_ralphing = False
            if item.get("type") != "function_call":
                continue
            if item["call_id"] not in self.session.failed_call_ids:
                tools[item["name"]].replay(item["arguments"])

        del self.session.transcript[checkpoint_index:]
        offer = self._stamp_offer()
        self.interviewer.offered_ralphing = False
        self.update_session(
            active_topic_id=self.interviewer.active_topic_id,
            interview=self.interviewer.history,
            transcript=self.session.transcript,
            **offer,
        )
        self.logger.info("rewound checkpoint=%d interview_items=%d", checkpoint_index, history_index)

    def ralph(self, cancelled: Event | None = None) -> Generator[TurnEvent]:
        checkpoint = self._capture_checkpoint(len(self.interviewer.history))
        # A run reports into the turn the user is looking at, since its
        # rows and its reply answer the message that turn opened with.
        if self.session.transcript:
            self.session.transcript[-1].work = "generation"
        else:
            self.session.transcript.append(Turn(message="", items=[], work="generation"))
        yield from self._report_turn(self._generate_specs(cancelled), checkpoint, cancelled)

    def restore(self) -> list[Turn]:
        if not self.workspace.session_file.exists():
            self.logger.info("restore_skipped reason=no_session_file")
            return []
        try:
            self.session = Session.model_validate_json(self.workspace.session_file.read_bytes())
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
        self.logger.info("restored interview_items=%d", len(self.session.interview))
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
        # A session saved before its first turn stored no interview at
        # all, and a stored one opens with the system prompt of the run
        # that wrote it, which the running one supersedes.
        if not self.session.interview:
            return self.interviewer.history
        return [self.interviewer.history[0], *cast("ResponseInputParam", self.session.interview[1:])]

    def _find_prompts(self) -> list[int]:
        return [
            index
            for index, item in enumerate(self.interviewer.history)
            if cast("dict[str, Any]", item).get("role") == "user"
        ]

    def _capture_checkpoint(self, history_length: int) -> Checkpoint:
        return Checkpoint(history_length, self.notebook.graph.model_copy(deep=True), self.interviewer.active_topic_id)

    def _generate_specs(self, cancelled: Event | None) -> Generator[AgentEvent]:
        result = yield from specs_generation.generate(self.settings, cancelled)
        # A run the user stopped reached no conclusion to report, and
        # spent nothing to reach it: the offer stands, and the model
        # hears about a run it would have nothing to say about.
        if result is None:
            return

        # An item joins the history for good, so it states what
        # happened and nothing else: what to do about it holds beyond
        # the moment, and what holds beyond the moment is the prompt's.
        if isinstance(result, str):
            workflow_result = f"Specification generation succeeded in Git commit {result}."
        else:
            workflow_result = prompt.render(specification_generation_ambiguities=result.ambiguities)
        report: ResponseInputItemParam = {"role": "system", "content": workflow_result}
        self.interviewer.history.append(report)
        # Past the report the run is spent, whatever the reply to it
        # does: what asking again re-runs from here is that reply, and
        # never the run the project already holds the commit of.
        self.session.transcript[-1].work = "reply"
        # A run that reached a report consumed the notes it was offered,
        # whatever it concluded about them; one that never got there
        # leaves the offer standing for the user to spend again.
        self.update_session(interview=self.interviewer.history, ready_graph=None)
        yield from self.interviewer.respond(cancelled)

    # The one place a turn is written down, and the one place it ends.
    # Both views read this recording rather than deriving their own, so
    # the restored conversation is the one the user watched.
    def _report_turn(
        self, events: Generator[AgentEvent], checkpoint: Checkpoint, cancelled: Event | None
    ) -> Generator[TurnEvent]:
        turn = self.session.transcript[-1]
        start = len(turn.items)
        open_rows: list[ToolCallStarted] = []
        failure: Exception | None = None
        try:
            for event in events:
                _record_event(turn, start, open_rows, event)
                yield event
        except Exception as error:
            # What the user already saw stays written down; what the
            # turn changed behind it does not.
            self._roll_back(checkpoint)
            self.logger.exception("turn_failed")
            failure = error
        finally:
            events.close()

        stopped = cancelled is not None and cancelled.is_set()
        replied = any(item.type == "assistant" for item in turn.items[start:])
        if isinstance(failure, UsageLimitError):
            ending: Ending = "exhausted"
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

        # A row still open when the turn ended is closed here, with a
        # real event, so the recording and the renderer take it through
        # the one path they already have for a call that finished.
        for row in reversed(open_rows):
            closing = ToolCallFinished(row.call_id, row.label, "stopped" if stopped else "failed", depth=row.depth)
            if not row.depth:
                turn.items.append(Item(type="tool", text=row.label, symbol=row.symbol, outcome=closing.outcome))
            yield closing
        turn.ending = ending
        turn.detail = str(failure) if failure is not None else ""
        offer = self._stamp_offer()
        self.interviewer.offered_ralphing = False
        self.update_session(
            active_topic_id=self.interviewer.active_topic_id,
            interview=self.interviewer.history,
            transcript=self.session.transcript,
            **offer,
        )
        self.logger.info("turn_finished ending=%s interview_items=%d", ending, len(self.interviewer.history))
        yield TurnFinished(ending, turn.detail)

    # What opened the turn outlives it, so the user can retry it.
    def _roll_back(self, checkpoint: Checkpoint) -> None:
        self.interviewer.history = self.interviewer.history[: checkpoint.history_length + 1]
        self.notebook.restore(checkpoint.graph)
        self.interviewer.active_topic_id = checkpoint.active_topic_id
        self.interviewer.offered_ralphing = False

    # An offer is the notes it was made about, so the turn stamps it
    # with the notebook it ends holding: the notes the model connects
    # right after offering are part of what it offered. A turn that
    # made no offer leaves the field out rather than clearing whatever
    # an earlier turn stamped.
    def _stamp_offer(self) -> dict[str, Graph]:
        if not self.interviewer.offered_ralphing:
            return {}
        return {"ready_graph": self.notebook.graph.model_copy(deep=True)}


def _record_event(turn: Turn, start: int, open_rows: list[ToolCallStarted], event: AgentEvent) -> None:
    match event:
        case ToolCallStarted():
            open_rows.append(event)
        case ToolCallFinished():
            symbol = DEFAULT_SYMBOL
            for index, row in enumerate(open_rows):
                if row.call_id == event.call_id:
                    symbol = row.symbol
                    # Whatever opened after a row is nested under it,
                    # so closing that row closes them with it.
                    del open_rows[index:]
                    break
            if not event.depth:
                turn.items.append(
                    Item(type="tool", text=event.label, symbol=symbol, outcome=event.outcome, detail=event.detail)
                )
        case TextDelta():
            _record_text(turn, start, "assistant", event.text)
        case ReasoningDelta():
            _record_text(turn, start, "reasoning", event.text)


def _record_text(turn: Turn, start: int, type_: Literal["assistant", "reasoning"], text: str) -> None:
    # Only an item this turn appended can be extended; one left by an
    # earlier turn is finished text.
    if len(turn.items) > start and turn.items[-1].type == type_:
        turn.items[-1].text += text
    else:
        turn.items.append(Item(type=type_, text=text))
