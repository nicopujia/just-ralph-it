import json
import shutil
from collections.abc import Generator
from threading import Lock
from typing import Literal, NamedTuple

from openai import BaseModel as OpenAIModel

from .agents import ChatEvent, Interviewer
from .agents.shared.events import ModelIterationCompleted
from .settings import Settings


class InterviewItem(NamedTuple):
    type: Literal["user", "assistant", "reasoning", "tool"]
    text: str
    symbol: str | None = None


class RestoredState(NamedTuple):
    items: list[InterviewItem]
    show_thinking_blocks: bool


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
        self.settings = settings
        self.interviewer = Interviewer(settings)
        self.interviewer_lock = Lock()
        self.active_interviewer: Interviewer | None = None

        self.base_dir = settings.cwd / ".jri"
        self.gitignore_file = self.base_dir / ".gitignore"
        self.state_file = self.base_dir / "state.json"
        self.show_thinking_blocks = False

        if settings.force:
            shutil.rmtree(self.base_dir)

        self.base_dir.mkdir(exist_ok=True, parents=True)
        self.gitignore_file.write_text(self.state_file.name)

    def chat(self, message: str) -> Generator[ChatEvent]:
        """Send a message and persist the full interview context.

        Yields:
            Streamed chat events from the interviewer.
        """
        interviewer = self.interviewer
        interviewer.cancellation_event.clear()
        with self.interviewer_lock:
            self.active_interviewer = interviewer
        try:
            for event in interviewer.send_message(message):
                if isinstance(event, ModelIterationCompleted):
                    if not self._save_iteration(interviewer):
                        return
                    continue
                yield event
        finally:
            with self.interviewer_lock:
                if self.active_interviewer is interviewer:
                    self.active_interviewer = None

    def _save_iteration(self, interviewer: Interviewer) -> bool:
        with self.interviewer_lock:
            if interviewer is not self.interviewer or interviewer.cancellation_event.is_set():
                return False
            self._write_state()
        return True

    def cancel(self) -> Interviewer | None:
        """Cancel and detach the active interviewer turn.

        Returns:
            The detached interviewer, or None if no turn is active.
        """

        with self.interviewer_lock:
            interviewer = self.active_interviewer
            if interviewer is None:
                return None
            interviewer.cancel()
            interviewer.finish_cancelled_iteration()
            interviewer.ctx.append({"role": "system", "content": "[Stopped by user]"})
            self.interviewer = Interviewer(self.settings)
            self.interviewer.ctx = list(interviewer.ctx)
            self.active_interviewer = None
            self._write_state()
        return interviewer

    def set_show_thinking_blocks(self, *, show: bool) -> None:
        """Persist whether reasoning blocks are visible."""

        with self.interviewer_lock:
            self.show_thinking_blocks = show
            self._write_state()

    def _write_state(self) -> None:
        interview_json = [
            (item.model_dump(mode="json") if isinstance(item, OpenAIModel) else item) for item in self.interviewer.ctx
        ]
        state = {"interview": interview_json, "show_thinking_blocks": self.show_thinking_blocks}
        self.state_file.write_text(f"{json.dumps(state, indent=2)}\n")

    def restore(self) -> RestoredState:
        """Restore interview session if present.

        Returns:
            Interview items and runtime state.
        """
        if not self.state_file.exists():
            return RestoredState(items=[], show_thinking_blocks=False)
        state = json.loads(self.state_file.read_text())
        self.interviewer.ctx = state["interview"]
        self.show_thinking_blocks = state["show_thinking_blocks"]
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
        return RestoredState(items, self.show_thinking_blocks)
