import logging
from collections.abc import Generator
from functools import cached_property
from threading import Event, Lock
from typing import Any, Literal, NamedTuple, cast

from openai.types.responses import ResponseInputParam
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jri.lib import files

from . import paths
from .ai import DEFAULT_SYMBOL, ChatEvent, Interviewer, SpecsGen, Tool
from .exceptions import PersistenceError
from .notes import Graph, Notebook, TopicId
from .settings import Settings


def read_turns(history: ResponseInputParam, tools: list[Tool], session: "Session") -> list["Turn"]:
    """Read a persisted interview as the turns the user saw.

    Returns:
        One turn per user prompt, holding what the interviewer
        answered it with.
    """

    tools_by_name = {tool.name: tool for tool in tools}
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
                InterviewItem("tool", tool.format_label(tool.finished_label, item["arguments"]), tool.symbol)
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
    elif turns and session.stopped_turn and all(item.type != "assistant" for item in turns[-1].items):
        turns[-1].items.append(InterviewItem("stopped"))
    return turns


class InterviewItem(NamedTuple):
    type: Literal["assistant", "reasoning", "tool", "error", "stopped"]
    text: str = ""
    symbol: str = DEFAULT_SYMBOL


class Turn(NamedTuple):
    message: str
    items: list[InterviewItem]


class Session(BaseModel):
    """Persisted terminal session."""

    active_topic_id: TopicId
    initial_graph: Graph
    interview: list[dict[str, Any]] = Field(default_factory=list)
    failed_call_ids: list[str] = Field(default_factory=list)
    failed_turn_error: str | None = None
    stopped_turn: bool = False
    ready_to_ralph: bool = False
    active_spec_commit: str | None = None
    show_thinking_blocks: bool = False

    model_config = ConfigDict(extra="forbid")


class Conversation:
    def __init__(self, settings: Settings) -> None:
        """Bind a conversation to the workspace `Workspace.create` made.

        Nothing here reaches the filesystem, so a command that fails
        before it reads or writes anything leaves the project alone.

        Directory structure:
        ```
            $CWD/.jri/
                .gitignore
                config.yaml
                session.json
                notebook.yaml
                visualization.html
                logs/
                    YYYY-MM-DD_HH-MM-SS.log
                    ...
                specs/
        ```
        """

        self.notebook_file = settings.cwd / paths.NOTEBOOK_FILE
        self.session_file = settings.cwd / paths.SESSION_FILE

        self.session_lock = Lock()
        self.settings = settings
        self.logger = logging.getLogger(__name__)
        self.logger.info("initialized cwd=%r", settings.cwd)

    @cached_property
    def notebook(self) -> Notebook:
        """Open the notes the first time a command needs them.

        Returns:
            The notebook this conversation reads and writes.
        """

        return Notebook(self.notebook_file)

    @cached_property
    def session(self) -> Session:
        """Start a session the first time a command needs one.

        Returns:
            A session anchored to the notebook's initial topic.
        """

        return Session(
            active_topic_id=self.notebook.initial_topic.id, initial_graph=self.notebook.graph.model_copy(deep=True)
        )

    @cached_property
    def interviewer(self) -> Interviewer:
        """Build the interviewer the first time one is needed.

        Commands that only read the notes never reach the provider.

        Returns:
            The interviewer writing into this conversation's notebook.
        """

        return Interviewer(self.settings, self.notebook, lambda ready: self.update_session(ready_to_ralph=ready))

    def chat(self, message: str, cancelled: Event | None = None) -> Generator[ChatEvent]:
        """Send a message and persist the full interview context.

        Yields:
            Streamed chat events from the interviewer.
        """
        self.logger.info("chat_started")
        self.logger.debug("chat_message message=%r", message)
        checkpoint = (
            len(self.interviewer.history),
            self.notebook.graph.model_copy(deep=True),
            self.interviewer.active_topic_id,
            self.session.ready_to_ralph,
        )
        yield from self._respond(message, checkpoint, cancelled)

    def retry(self, cancelled: Event | None = None) -> Generator[ChatEvent]:
        """Retry the latest failed message from its original checkpoint.

        Yields:
            Streamed chat events from the interviewer.
        """
        checkpoint = (
            len(self.interviewer.history) - 1,
            self.notebook.graph.model_copy(deep=True),
            self.interviewer.active_topic_id,
            self.session.ready_to_ralph,
        )
        message = cast("dict[str, str]", self.interviewer.history.pop())["content"]
        yield from self._respond(message, checkpoint, cancelled)

    def rewind(self, checkpoint_index: int) -> None:
        """Rewind conversation and notes to a user prompt."""

        history_index = [
            index
            for index, item in enumerate(self.interviewer.history)
            if cast("dict[str, Any]", item).get("role") == "user"
        ][checkpoint_index]
        self.interviewer.history = self.interviewer.history[:history_index]
        self.notebook.restore(self.session.initial_graph)
        self.interviewer.active_topic_id = self.notebook.initial_topic.id
        self.session = self.session.model_copy(update={"ready_to_ralph": False})

        tools = {tool.name: tool for tool in self.interviewer.tools}
        for raw_item in self.interviewer.history:
            item = cast("dict[str, Any]", raw_item)
            if item.get("type") != "function_call":
                continue
            tool = tools[item["name"]]
            if not tool.read_only and item["call_id"] not in self.session.failed_call_ids:
                list(tool.invoke(item["arguments"]))

        self._save_turn()
        self.logger.info("rewound checkpoint=%d interview_items=%d", checkpoint_index, history_index)

    def ralph(self) -> Generator[ChatEvent]:
        """Generate specifications after explicit user confirmation.

        Yields:
            Specification progress and the Interviewer's response.
        """

        self.update_session(ready_to_ralph=False)
        try:
            result = yield from SpecsGen(self.settings).generate(self.session.active_spec_commit)
        except BaseException:
            self.update_session(ready_to_ralph=True)
            raise

        if isinstance(result, str):
            self.update_session(active_spec_commit=result)
            workflow_result = (
                f"Specification generation succeeded in Git commit {result}. "
                "Confirm completion concisely and do not show the Just Ralph It button again."
            )
        else:
            workflow_result = (
                "Specification generation found these behavioral ambiguities. Discuss them with the user and update "
                "the notebook before offering Just Ralph It again:\n"
                + "\n".join(f"- {item}" for item in result.ambiguities)
            )
        self.interviewer.history.append({"role": "system", "content": workflow_result})
        self.update_session(interview=self.interviewer.history)
        yield from self.interviewer.respond()
        self._save_turn()

    def restore(self) -> list[Turn]:
        """Restore interview session if present.

        Returns:
            The interview turns to show again.

        Raises:
            PersistenceError: If the session file is invalid.
        """
        if not self.session_file.exists():
            self.logger.info("restore_skipped reason=no_session_file")
            return []
        try:
            self.session = Session.model_validate_json(self.session_file.read_text())
            topics = {topic.id: topic for topic in self.notebook.graph.topics if topic.status != "trashed"}
            topics[self.session.active_topic_id]
            self.interviewer.history, self.interviewer.active_topic_id = (
                cast(
                    "ResponseInputParam",
                    [{"role": "system", "content": self.interviewer.prompt}, *self.session.interview[1:]],
                ),
                self.session.active_topic_id,
            )
            turns = read_turns(self.interviewer.history, self.interviewer.tools, self.session)
        except (OSError, ValidationError, LookupError, TypeError) as error:
            raise PersistenceError(
                f"Invalid session file `{self.session_file}`. Delete it to start a new conversation, "
                "or run `jri init --force` to reset the whole workspace, notes included."
            ) from error
        self.interviewer.failed_call_ids = list(self.session.failed_call_ids)
        self.logger.info("restored interview_items=%d", len(self.session.interview))
        return turns

    def update_session(self, **values: object) -> None:
        """Persist trusted values in the current session.

        Raises:
            PersistenceError: If the session file cannot be written.
        """

        with self.session_lock:
            session = self.session.model_copy(
                update={"failed_call_ids": list(self.interviewer.failed_call_ids), **values}
            )
            try:
                files.write_atomically(self.session_file, session.model_dump_json())
            except OSError as error:
                self.logger.exception("session_write_failed path=%r", self.session_file)
                raise PersistenceError(
                    f"Could not save the session file `{self.session_file}`: {error.strerror}"
                ) from error
            self.session = session
        self.logger.info("session_updated fields=%r interview_items=%d", list(values), len(self.session.interview))

    def _respond(
        self, message: str, checkpoint: tuple[int, Graph, str, bool], cancelled: Event | None
    ) -> Generator[ChatEvent]:
        try:
            yield from self.interviewer.send_message(message, cancelled)
            self._save_turn(stopped=cancelled is not None and cancelled.is_set())
        except Exception:
            self.interviewer.history = self.interviewer.history[: checkpoint[0]]
            self.notebook.restore(checkpoint[1])
            self.interviewer.active_topic_id = checkpoint[2]
            self.session = self.session.model_copy(update={"ready_to_ralph": checkpoint[3]})
            self.interviewer.history.append({"role": "user", "content": message})
            self.update_session(active_topic_id=self.interviewer.active_topic_id, interview=self.interviewer.history)
            self.logger.exception("chat_rolled_back")
            raise
        self.logger.info("chat_finished interview_items=%d", len(self.interviewer.history))

    def _save_turn(self, *, stopped: bool = False) -> None:
        """Persist the interview, clearing the previous turn's marks."""

        self.update_session(
            active_topic_id=self.interviewer.active_topic_id,
            interview=self.interviewer.history,
            failed_turn_error=None,
            stopped_turn=stopped,
        )
