import json
import logging
import shutil
from collections.abc import Generator
from datetime import datetime
from threading import Lock
from typing import Any, Literal, NamedTuple, cast

from .agents import ChatEvent, Interviewer
from .notes import Notebook
from .settings import Settings


class InterviewItem(NamedTuple):
    type: Literal["user", "assistant", "reasoning", "tool"]
    text: str
    symbol: str | None = None


class Service:
    def __init__(self, settings: Settings) -> None:
        """Load settings, configure logging, and set base directory up.

        Directory structure:
        ```
            $CWD/.jri/
                .gitignore
                state.json
                graph.json
                logs/
                    YYYY-MM-DD_HH-MM-SS.log
                    ...
        ```
        """
        self.base_dir = settings.cwd / ".jri"
        self.logs_dir = self.base_dir / "logs"
        self.gitignore_file = self.base_dir / ".gitignore"
        self.graph_file = self.base_dir / "graph.json"
        self.graph_visualization_file = self.base_dir / "graph.html"
        self.state_file = self.base_dir / "state.json"

        self.state_lock = Lock()
        self.state: dict[str, Any] = {"interview": [], "explorations": {}, "show_thinking_blocks": False}

        if settings.force:
            shutil.rmtree(self.base_dir)

        self.base_dir.mkdir(exist_ok=True, parents=True)
        self.logs_dir.mkdir(exist_ok=True, parents=True)
        ignored_paths = [self.state_file, self.logs_dir, self.graph_visualization_file]
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
        self.interviewer = Interviewer(settings, Notebook(self.graph_file))

    def chat(self, message: str) -> Generator[ChatEvent]:
        """Send a message and persist the full interview context.

        Yields:
            Streamed chat events from the interviewer.
        """
        self.logger.info("chat_started")
        self.logger.debug("chat_message message=%r", message)
        yield from self.interviewer.send_message(message)
        self.update_state(interview=self.interviewer.history, explorations=self.interviewer.explorations)
        self.logger.info("chat_finished interview_items=%d", len(self.interviewer.history))

    def restore(self) -> tuple[list[InterviewItem], bool]:
        """Restore interview session if present.

        Returns:
            Interview items and runtime state.
        """
        if not self.state_file.exists():
            self.logger.info("restore_skipped reason=no_state_file")
            return [], False
        self.state = json.loads(self.state_file.read_text())
        self.logger.info("restored interview_items=%d", len(self.state["interview"]))
        self.interviewer.history = list(self.state["interview"])
        self.interviewer.explorations = self.state["explorations"]
        tools_by_name = {tool.name: tool for tool in self.interviewer.tools}
        items: list[InterviewItem] = []
        for raw_item in self.interviewer.history:
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
        return items, bool(self.state["show_thinking_blocks"])

    def update_state(self, **values: object) -> None:
        """Persist trusted values in the current state."""

        with self.state_lock:
            self.state.update(values)
            self.state_file.write_text(f"{json.dumps(self.state, indent=2)}\n")
        self.logger.info("state_updated fields=%r interview_items=%d", list(values), len(self.state["interview"]))
