import logging
from collections.abc import Generator
from functools import cached_property
from threading import Event, Lock
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

from openai.types.responses import ResponseInputParam
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jri.lib import files

from .ai import DEFAULT_SYMBOL, ChatEvent, Interviewer, Tool, specs_generation
from .exceptions import PersistenceError
from .notes import Graph, Notebook, TopicId
from .settings import Settings
from .workspace import Workspace

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputItemParam


def read_turns(history: ResponseInputParam, tools: list[Tool], session: "Session") -> list["Turn"]:
    tools_by_name = {tool.name: tool for tool in tools}
    failed_call_ids = set(session.failed_call_ids)
    turns: list[Turn] = []
    for raw_item in history[2:]:
        item = cast("dict[str, Any]", raw_item)
        if item.get("role") == "user" and item.get("content"):
            turns.append(Turn(cast("str", item["content"]), []))
            continue
        if not turns:
            continue
        if item.get("type") == "function_call":
            tool = tools_by_name[item["name"]]
            turns[-1].items.append(
                InterviewItem(
                    "tool",
                    tool.format_label(tool.finished_label, item["arguments"]),
                    tool.symbol,
                    item["call_id"] in failed_call_ids,
                )
            )
            continue
        if item.get("type") == "reasoning":
            summary = "".join(part["text"] for part in item["summary"] if part["type"] == "summary_text")
            reasoning = "".join(part["text"] for part in item.get("content", []) if part["type"] == "reasoning_text")
            if summary or reasoning:
                turns[-1].items.append(InterviewItem("reasoning", summary or reasoning))
            continue
        if item.get("role") != "assistant" or "content" not in item:
            continue
        content = item["content"]
        text = (
            content
            if isinstance(content, str)
            else "".join(part["text"] for part in content if part["type"] == "output_text")
        )
        if text:
            turns[-1].items.append(InterviewItem("assistant", text))
    if turns and session.failed_turn_error:
        turns[-1].items.append(InterviewItem("error", session.failed_turn_error))
    elif session.stopped_turn is not None and session.stopped_turn < len(turns):
        stopped = turns[session.stopped_turn]
        if all(item.type != "assistant" for item in stopped.items):
            stopped.items.append(InterviewItem("stopped"))
    return turns


class InterviewItem(NamedTuple):
    type: Literal["assistant", "reasoning", "tool", "error", "stopped"]
    text: str = ""
    symbol: str = DEFAULT_SYMBOL
    failed: bool = False


class Turn(NamedTuple):
    message: str
    items: list[InterviewItem]


class Checkpoint(NamedTuple):
    history_length: int
    graph: Graph
    active_topic_id: TopicId
    ready_to_ralph: bool


class Session(BaseModel):
    active_topic_id: TopicId
    initial_graph: Graph
    interview: list[dict[str, Any]] = Field(default_factory=list)
    failed_call_ids: list[str] = Field(default_factory=list)
    failed_turn_error: str | None = None
    stopped_turn: int | None = None
    ready_to_ralph: bool = False
    active_spec_commit: str | None = None
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
        return Interviewer(self.settings, self.notebook, lambda ready: self.update_session(ready_to_ralph=ready))

    def chat(self, message: str, cancelled: Event | None = None) -> Generator[ChatEvent]:
        self.logger.info("chat_started")
        self.logger.debug("chat_message message=%r", message)
        checkpoint = self._capture_checkpoint(len(self.interviewer.history))
        yield from self._respond(message, checkpoint, cancelled)

    def retry(self, cancelled: Event | None = None) -> Generator[ChatEvent]:
        # Whatever the turn left behind goes with it: only the prompt
        # that opened it is sent again.
        history_index = self._find_prompts()[-1]
        message = cast("str", cast("dict[str, Any]", self.interviewer.history[history_index])["content"])
        checkpoint = self._capture_checkpoint(history_index)
        self.interviewer.history = self.interviewer.history[:history_index]
        yield from self._respond(message, checkpoint, cancelled)

    def rewind(self, checkpoint_index: int) -> None:
        history_index = self._find_prompts()[checkpoint_index]
        self.interviewer.history = self.interviewer.history[:history_index]
        # The calls kept are replayed below, so they have to name their
        # notes exactly as the calls that referenced them expect.
        self.notebook.restore(self.session.initial_graph, reuse_note_ids=True)
        self.interviewer.active_topic_id = self.notebook.initial_topic.id
        self.session = self.session.model_copy(update={"ready_to_ralph": False})

        tools = {tool.name: tool for tool in self.interviewer.tools}
        for raw_item in self.interviewer.history:
            item = cast("dict[str, Any]", raw_item)
            if item.get("type") != "function_call":
                continue
            if item["call_id"] not in self.session.failed_call_ids:
                tools[item["name"]].replay(item["arguments"])

        self._save_turn()
        self.logger.info("rewound checkpoint=%d interview_items=%d", checkpoint_index, history_index)

    def ralph(self) -> Generator[ChatEvent]:
        self.update_session(ready_to_ralph=False)
        try:
            result = yield from specs_generation.generate(self.settings, self.session.active_spec_commit)
        except BaseException:
            self.update_session(ready_to_ralph=True)
            raise

        if isinstance(result, str):
            self.update_session(active_spec_commit=result)
            workflow_result = (
                f"Specification generation succeeded in Git commit {result}. Confirm completion concisely."
            )
        else:
            workflow_result = (
                "Specification generation found these behavioral ambiguities. Discuss them with the user and update "
                "the notebook before offering Just Ralph It again:\n"
                + "\n".join(f"- {item}" for item in result.ambiguities)
            )
        report: ResponseInputItemParam = {"role": "system", "content": workflow_result}
        checkpoint = self._capture_checkpoint(len(self.interviewer.history))
        self.interviewer.history.append(report)
        self.update_session(interview=self.interviewer.history)
        try:
            yield from self.interviewer.respond()
            self._save_turn()
        except Exception:
            self._roll_back(checkpoint, report)
            self.logger.exception("ralph_rolled_back")
            raise

    def restore(self) -> list[Turn]:
        if not self.workspace.session_file.exists():
            self.logger.info("restore_skipped reason=no_session_file")
            return []
        try:
            self.session = Session.model_validate_json(self.workspace.session_file.read_text())
            topics = {topic.id: topic for topic in self.notebook.graph.topics if topic.status != "trashed"}
            topics[self.session.active_topic_id]
            history = self._read_interview()
            turns = read_turns(history, self.interviewer.tools, self.session)
        except (OSError, ValidationError, LookupError, TypeError) as error:
            raise PersistenceError(
                f"Invalid session file `{self.workspace.session_file}`. Delete it to start a new conversation, "
                "or run `jri init --force` to reset the whole workspace, notes included."
            ) from error
        self.interviewer.history = history
        self.interviewer.active_topic_id = self.session.active_topic_id
        self.interviewer.failed_call_ids = list(self.session.failed_call_ids)
        self.logger.info("restored interview_items=%d", len(self.session.interview))
        return turns

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
        return Checkpoint(
            history_length,
            self.notebook.graph.model_copy(deep=True),
            self.interviewer.active_topic_id,
            self.session.ready_to_ralph,
        )

    def _respond(self, message: str, checkpoint: Checkpoint, cancelled: Event | None) -> Generator[ChatEvent]:
        try:
            yield from self.interviewer.send_message(message, cancelled)
            self._save_turn(stopped=cancelled is not None and cancelled.is_set())
        except Exception:
            self._roll_back(checkpoint, {"role": "user", "content": message})
            self.logger.exception("chat_rolled_back")
            raise
        self.logger.info("chat_finished interview_items=%d", len(self.interviewer.history))

    # What opened the turn outlives it, so the user can retry it.
    def _roll_back(self, checkpoint: Checkpoint, opening: "ResponseInputItemParam") -> None:
        self.interviewer.history = self.interviewer.history[: checkpoint.history_length]
        self.notebook.restore(checkpoint.graph)
        self.interviewer.active_topic_id = checkpoint.active_topic_id
        self.session = self.session.model_copy(update={"ready_to_ralph": checkpoint.ready_to_ralph})
        self.interviewer.history.append(opening)
        self.update_session(active_topic_id=self.interviewer.active_topic_id, interview=self.interviewer.history)

    def _save_turn(self, *, stopped: bool = False) -> None:
        self.update_session(
            active_topic_id=self.interviewer.active_topic_id,
            interview=self.interviewer.history,
            failed_turn_error=None,
            # Naming the turn that was stopped keeps the mark on it when
            # a later turn ends without one of its own.
            stopped_turn=len(self._find_prompts()) - 1 if stopped else None,
        )
