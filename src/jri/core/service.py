import json
import shutil
from collections.abc import Generator
from threading import Lock
from typing import Any, Literal, NamedTuple

from openai import BaseModel as OpenAIModel

from .agents import ChatEvent, Interviewer
from .settings import Settings


class InterviewItem(NamedTuple):
    type: Literal["user", "assistant", "reasoning", "tool"]
    text: str
    symbol: str | None = None


class Service:
    def __init__(self, settings: Settings) -> None:
        """Load settings and set base directory up.

        Directory structure:
        ```
            $CWD/.jri/
                .gitignore
                state.json
        ```
        """
        self.interviewer = Interviewer(settings)

        self.base_dir = settings.cwd / ".jri"
        self.gitignore_file = self.base_dir / ".gitignore"
        self.state_file = self.base_dir / "state.json"
        self.state_lock = Lock()
        self.state: dict[str, Any] = {"interview": [], "show_thinking_blocks": False}

        if settings.force:
            shutil.rmtree(self.base_dir)

        self.base_dir.mkdir(exist_ok=True, parents=True)
        self.gitignore_file.write_text(self.state_file.name)

    def chat(self, message: str) -> Generator[ChatEvent]:
        """Send a message and persist the full interview context.

        Yields:
            Streamed chat events from the interviewer.
        """
        yield from self.interviewer.send_message(message)
        interview = [
            (item.model_dump(mode="json") if isinstance(item, OpenAIModel) else item) for item in self.interviewer.ctx
        ]
        self.update_state(interview=interview)

    def restore(self) -> tuple[list[InterviewItem], bool]:
        """Restore interview session if present.

        Returns:
            Interview items and runtime state.
        """
        if not self.state_file.exists():
            return [], False
        self.state = json.loads(self.state_file.read_text())
        self.interviewer.ctx = self.state["interview"]
        tools_by_name = {tool.name: tool for tool in self.interviewer.tools}
        items: list[InterviewItem] = []
        for item in self.interviewer.ctx:
            if item.get("role") == "system":
                continue
            item_type = item.get("type")
            if item_type == "function_call":
                tool = tools_by_name[item["name"]]
                items.append(
                    InterviewItem("tool", tool.format_label(tool.finished_label, item["arguments"]), tool.symbol)
                )
                continue
            if item_type == "reasoning":
                summary = "".join(part["text"] for part in item["summary"] if part["type"] == "summary_text")
                reasoning = "".join(
                    part["text"] for part in item.get("content", []) if part["type"] == "reasoning_text"
                )
                if summary or reasoning:
                    items.append(InterviewItem("reasoning", summary or reasoning))
                continue
            if item_type not in {None, "message"}:
                continue
            content = item["content"]
            if isinstance(content, list):
                content = "".join(part["text"] for part in content if part["type"] == "output_text")
            if content:
                items.append(InterviewItem(item["role"], content))
        return items, bool(self.state["show_thinking_blocks"])

    def update_state(self, **values: object) -> None:
        """Persist trusted values in the current state."""

        with self.state_lock:
            self.state.update(values)
            self.state_file.write_text(f"{json.dumps(self.state, indent=2)}\n")
