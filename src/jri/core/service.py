import logging
import shutil
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event, Lock
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jri.core import paths

from .ai import ChatEvent, Interviewer, SpecsGen
from .exceptions import PersistenceError
from .notes import Graph, Notebook, TopicId
from .settings import Settings, initialize_workspace

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputParam


class InterviewItem(NamedTuple):
    type: Literal["user", "assistant", "reasoning", "tool"]
    text: str
    symbol: str | None = None


class Session(BaseModel):
    """Persisted terminal session."""

    active_topic_id: TopicId
    initial_graph: Graph
    interview: list[dict[str, Any]] = Field(default_factory=list)
    ready_to_ralph: bool = False
    active_spec_commit: str | None = None
    show_thinking_blocks: bool = False

    model_config = ConfigDict(extra="forbid")


class Service:
    def __init__(self, settings: Settings) -> None:
        """Load settings, configure logging, and set base directory up.

        Directory structure:
        ```
            $CWD/.jri/
                .gitignore
                config.yaml
                secrets.yaml
                session.json
                notebook.yaml
                logs/
                    YYYY-MM-DD_HH-MM-SS.log
                    ...
        ```
        """
        self.base_dir = settings.cwd / paths.WORKSPACE_DIR
        self.logs_dir = settings.cwd / paths.LOGS_DIR
        self.gitignore_file = settings.cwd / paths.GITIGNORE_FILE
        self.notebook_file = settings.cwd / paths.NOTEBOOK_FILE
        self.visualization_file = settings.cwd / paths.VISUALIZATION_FILE
        self.session_file = settings.cwd / paths.SESSION_FILE

        self.session_lock = Lock()
        self.settings = settings

        if settings.force:
            self.notebook_file.unlink(missing_ok=True)
            self.session_file.unlink(missing_ok=True)
            self.visualization_file.unlink(missing_ok=True)
            for directory in (self.logs_dir, settings.cwd / paths.SPECS_DIR):
                if directory.exists():
                    shutil.rmtree(directory)

        initialize_workspace(settings.cwd)
        self.logs_dir.mkdir(exist_ok=True, parents=True)

        log_file = self.logs_dir / f"{datetime.now().astimezone().strftime('%Y-%m-%d_%H-%M-%S')}.log"
        application_logger = logging.getLogger("jri")
        application_logger.setLevel(settings.logging.level)
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"))
        application_logger.addHandler(handler)
        application_logger.propagate = False
        self.logger = logging.getLogger(__name__)
        self.logger.info("initialized cwd=%r force=%r", settings.cwd, settings.force)
        self.interviewer = Interviewer(
            settings, Notebook(self.notebook_file), lambda ready: self.update_session(ready_to_ralph=ready)
        )
        self.session = Session(
            active_topic_id=self.interviewer.active_topic_id,
            initial_graph=self.interviewer.notebook.graph.model_copy(deep=True),
        )

    def chat(self, message: str, cancelled: Event | None = None) -> Generator[ChatEvent]:
        """Send a message and persist the full interview context.

        Yields:
            Streamed chat events from the interviewer.
        """
        self.logger.info("chat_started")
        self.logger.debug("chat_message message=%r", message)
        checkpoint = (
            len(self.interviewer.history),
            self.interviewer.notebook.graph.model_copy(deep=True),
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
            self.interviewer.notebook.graph.model_copy(deep=True),
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
        self.interviewer.notebook.restore(self.session.initial_graph)
        self.interviewer.active_topic_id = self.interviewer.initial_topic.id
        self.session = self.session.model_copy(update={"ready_to_ralph": False})

        outputs = {
            item["call_id"]: item["output"]
            for raw_item in self.interviewer.history
            if (item := cast("dict[str, Any]", raw_item)).get("type") == "function_call_output"
        }
        tools = {tool.name: tool for tool in self.interviewer.tools}
        for raw_item in self.interviewer.history:
            item = cast("dict[str, Any]", raw_item)
            if item.get("type") != "function_call" or item["name"] in {"explore", "read_notes"}:
                continue
            output = outputs.get(item["call_id"])
            if not isinstance(output, str) or output.startswith(("Tool call cancelled.", "Tool call failed:")):
                continue
            list(tools[item["name"]].invoke(item["arguments"]))

        self.update_session(active_topic_id=self.interviewer.active_topic_id, interview=self.interviewer.history)
        self.logger.info("rewound checkpoint=%d interview_items=%d", checkpoint_index, history_index)

    def ralph(self) -> Generator[ChatEvent]:
        """Generate specifications after explicit user confirmation.

        Yields:
            Specification progress and the Interviewer's response.
        """

        self.update_session(ready_to_ralph=False)
        try:
            result = yield from SpecsGen(self.settings).generate(self.session.active_spec_commit)
        except Exception:
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
        self.update_session(active_topic_id=self.interviewer.active_topic_id, interview=self.interviewer.history)

    def restore(self) -> tuple[list[InterviewItem], bool]:
        """Restore interview session if present.

        Returns:
            Interview items and runtime session values.

        Raises:
            PersistenceError: If the session file is invalid.
        """
        if not self.session_file.exists():
            self.logger.info("restore_skipped reason=no_session_file")
            return [], False
        try:
            self.session = Session.model_validate_json(self.session_file.read_text())
            topics = {topic.id: topic for topic in self.interviewer.notebook.graph.topics if topic.status != "trashed"}
            topics[self.session.active_topic_id]
            self.interviewer.history, self.interviewer.active_topic_id = (
                cast(
                    "ResponseInputParam",
                    [{"role": "system", "content": self.interviewer.sys_prompt}, *self.session.interview[1:]],
                ),
                self.session.active_topic_id,
            )
            items = self._get_items()
        except (OSError, ValidationError, LookupError, TypeError) as error:
            raise PersistenceError(
                f"Invalid session file `{self.session_file}`. Run JRI with --force to reset it."
            ) from error
        self.logger.info("restored interview_items=%d", len(self.session.interview))
        return items, self.session.show_thinking_blocks

    def _get_items(self) -> list[InterviewItem]:
        tools_by_name = {tool.name: tool for tool in self.interviewer.tools}
        items: list[InterviewItem] = []
        for raw_item in self.interviewer.history[2:]:
            item = cast("dict[str, Any]", raw_item)
            if item.get("type") == "function_call":
                tool = tools_by_name[item["name"]]
                items.append(
                    InterviewItem("tool", tool.format_label(tool.finished_label, item["arguments"]), tool.symbol)
                )
                continue
            if item.get("type") == "reasoning":
                summary = "".join(part["text"] for part in item["summary"] if part["type"] == "summary_text")
                reasoning = "".join(
                    part["text"] for part in item.get("content", []) if part["type"] == "reasoning_text"
                )
                if summary or reasoning:
                    items.append(InterviewItem("reasoning", summary or reasoning))
                continue
            if "role" not in item or "content" not in item:
                continue
            role = item["role"]
            if role not in {"user", "assistant"}:
                continue
            content = item["content"]
            text = (
                content
                if isinstance(content, str)
                else "".join(part["text"] for part in content if part["type"] == "output_text")
            )
            if text:
                items.append(InterviewItem(role, text))
        return items

    def update_session(self, **values: object) -> None:
        """Persist trusted values in the current session."""

        with self.session_lock:
            session = self.session.model_copy(update=values)
            with NamedTemporaryFile("w", dir=self.base_dir, delete=False, encoding="utf-8") as file:
                file.write(f"{session.model_dump_json(indent=2)}\n")
            Path(file.name).replace(self.session_file)
            self.session = session
        self.logger.info("session_updated fields=%r interview_items=%d", list(values), len(self.session.interview))

    def _respond(
        self, message: str, checkpoint: tuple[int, Graph, str, bool], cancelled: Event | None
    ) -> Generator[ChatEvent]:
        try:
            yield from self.interviewer.send_message(message, cancelled)
            self.update_session(active_topic_id=self.interviewer.active_topic_id, interview=self.interviewer.history)
        except Exception:
            self.interviewer.history = self.interviewer.history[: checkpoint[0]]
            self.interviewer.notebook.restore(checkpoint[1])
            self.interviewer.active_topic_id = checkpoint[2]
            self.session = self.session.model_copy(update={"ready_to_ralph": checkpoint[3]})
            self.interviewer.history.append({"role": "user", "content": message})
            self.update_session(active_topic_id=self.interviewer.active_topic_id, interview=self.interviewer.history)
            self.logger.exception("chat_rolled_back")
            raise
        self.logger.info("chat_finished interview_items=%d", len(self.interviewer.history))
