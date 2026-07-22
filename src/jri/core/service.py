import logging
import shutil
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Event, Lock
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .agents import ChatEvent, Interviewer
from .exceptions import PersistenceError
from .notes import Graph, Notebook, TopicId
from .settings import Settings

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputParam


class InterviewItem(NamedTuple):
    type: Literal["user", "assistant", "reasoning", "tool"]
    text: str
    symbol: str | None = None


class Session(BaseModel):
    """Persisted terminal session."""

    active_topic_id: TopicId
    interview: list[dict[str, Any]] = Field(default_factory=list)
    show_thinking_blocks: bool = False

    model_config = ConfigDict(extra="forbid")


class Service:
    def __init__(self, settings: Settings) -> None:
        """Load settings, configure logging, and set base directory up.

        Directory structure:
        ```
            $CWD/.jri/
                .gitignore
                session.json
                notebook.yaml
                logs/
                    YYYY-MM-DD_HH-MM-SS.log
                    ...
        ```
        """
        self.base_dir = settings.cwd / ".jri"
        self.logs_dir = self.base_dir / "logs"
        self.gitignore_file = self.base_dir / ".gitignore"
        self.notebook_file = self.base_dir / "notebook.yaml"
        self.visualization_file = self.base_dir / "visualization.html"
        self.session_file = self.base_dir / "session.json"

        self.session_lock = Lock()

        if settings.force and self.base_dir.exists():
            shutil.rmtree(self.base_dir)

        self.base_dir.mkdir(exist_ok=True, parents=True)
        self.logs_dir.mkdir(exist_ok=True, parents=True)
        ignored_paths = [self.session_file, self.logs_dir, self.visualization_file]
        self.gitignore_file.write_text("\n".join([p.name for p in ignored_paths]) + "\n")

        log_file = self.logs_dir / f"{datetime.now().astimezone().strftime('%Y-%m-%d_%H-%M-%S')}.log"
        application_logger = logging.getLogger("jri")
        application_logger.setLevel(settings.logging_level)
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"))
        application_logger.addHandler(handler)
        application_logger.propagate = False
        self.logger = logging.getLogger(__name__)
        self.logger.info("initialized cwd=%r force=%r", settings.cwd, settings.force)
        self.interviewer = Interviewer(settings, Notebook(self.notebook_file))
        self.session = Session(active_topic_id=self.interviewer.active_topic_id)
        self.checkpoints: list[tuple[int, Graph, str]] = []

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
        )
        self.checkpoints.append(checkpoint)
        yield from self._respond(message, checkpoint, cancelled)

    def retry(self, cancelled: Event | None = None) -> Generator[ChatEvent]:
        """Retry the latest failed message from its original checkpoint.

        Yields:
            Streamed chat events from the interviewer.
        """
        checkpoint = (
            self.checkpoints[-1]
            if self.checkpoints
            else (
                len(self.interviewer.history) - 1,
                self.interviewer.notebook.graph.model_copy(deep=True),
                self.interviewer.active_topic_id,
            )
        )
        message = cast("dict[str, str]", self.interviewer.history.pop())["content"]
        yield from self._respond(message, checkpoint, cancelled)

    def rewind(self, checkpoint_index: int) -> None:
        """Rewind conversation and notes to a current-run checkpoint."""

        history_index, graph, active_topic_id = self.checkpoints[checkpoint_index]
        self.interviewer.history = self.interviewer.history[:history_index]
        self.interviewer.notebook.restore(graph)
        self.interviewer.active_topic_id = active_topic_id
        del self.checkpoints[checkpoint_index:]
        self.update_session(active_topic_id=active_topic_id, interview=self.interviewer.history)
        self.logger.info("rewound checkpoint=%d interview_items=%d", checkpoint_index, history_index)

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
        self, message: str, checkpoint: tuple[int, Graph, str], cancelled: Event | None
    ) -> Generator[ChatEvent]:
        try:
            yield from self.interviewer.send_message(message, cancelled)
            self.update_session(active_topic_id=self.interviewer.active_topic_id, interview=self.interviewer.history)
        except Exception:
            self.interviewer.history = self.interviewer.history[: checkpoint[0]]
            self.interviewer.notebook.restore(checkpoint[1])
            self.interviewer.active_topic_id = checkpoint[2]
            self.interviewer.history.append({"role": "user", "content": message})
            self.update_session(active_topic_id=self.interviewer.active_topic_id, interview=self.interviewer.history)
            self.logger.exception("chat_rolled_back")
            raise
        self.logger.info("chat_finished interview_items=%d", len(self.interviewer.history))
