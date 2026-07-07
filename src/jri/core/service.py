import json
import shutil
from collections.abc import Generator
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from .agents import Interviewer
from .agents.shared import ChatEvent, TextDelta, ToolCallStarted
from .notes import Notes
from .settings import Settings

if TYPE_CHECKING:
    from openai.types.responses import ResponseInputItemParam as InputItem


class InterviewItem(NamedTuple):
    type: Literal["user", "assistant", "tool"]
    text: str


class Service:
    def __init__(self, settings: Settings) -> None:
        """Load settings and set base directory up.

        Directory structure:
        ```
            $CWD/.jri/
                .gitignore
                notes.yaml
                interview.json
                state.json
        ```
        """
        self.base_dir = settings.cwd / ".jri"
        self.gitignore_file = self.base_dir / ".gitignore"
        self.notes_file = self.base_dir / "notes.yaml"
        self.state_file = self.base_dir / "state.json"
        self.interview_file = self.base_dir / "interview.json"

        if settings.force:
            if self.base_dir.is_dir() and not self.base_dir.is_symlink():
                shutil.rmtree(self.base_dir)
            else:
                self.base_dir.unlink(missing_ok=True)

        self.base_dir.mkdir(exist_ok=True, parents=True)

        self.gitignore_file.write_text("interview.json\nstate.json\n")
        self.notes = Notes(self.notes_file, self.state_file)
        self.interviewer = Interviewer(settings, self.notes)
        self.tool_finished_labels = {tool.name: tool.finished_label for tool in self.interviewer.tools}
        self.interview_items = self._load_interview_items()

    def chat(self, message: str) -> Generator[ChatEvent]:
        """Send a message and persist the visible interview transcript.

        Yields:
            Streamed chat events from the interviewer.
        """
        turn_items = [InterviewItem("user", message)]
        assistant_text: list[str] = []

        for chat_event in self.interviewer.send_message(message):
            if isinstance(chat_event, TextDelta):
                assistant_text.append(chat_event.text)
            elif isinstance(chat_event, ToolCallStarted):
                self._flush_assistant_text(assistant_text, turn_items)
                turn_items.append(InterviewItem("tool", chat_event.finished_label))
            yield chat_event

        self._flush_assistant_text(assistant_text, turn_items)
        self.interview_items.extend(turn_items)
        self._save_interview_items()

    def restore(self) -> list[InterviewItem]:
        """Restore interview session if present.

        Returns:
            List of interview items if present, which may be empty.
        """
        tail: list[InputItem] = [{"role": i.type, "content": i.text} for i in self.interview_items if i.type != "tool"]
        self.interviewer.rebuild_context(recent_tail=tail[-8:])
        return list(self.interview_items)

    def _load_interview_items(self) -> list[InterviewItem]:
        try:
            data: Any = json.loads(self.interview_file.read_text())
        except (OSError, ValueError):
            return []

        if not isinstance(data, list):
            return []

        items: list[InterviewItem] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            text = item.get("text")
            if not isinstance(text, str):
                continue
            match item_type:
                case "tool":
                    items.append(InterviewItem(item_type, self.tool_finished_labels.get(text, text)))
                case "user" | "assistant":
                    items.append(InterviewItem(item_type, text))
        return items

    def _save_interview_items(self) -> None:
        payload = [{"type": item.type, "text": item.text} for item in self.interview_items]
        self.interview_file.write_text(f"{json.dumps(payload, indent=2)}\n")

    @staticmethod
    def _flush_assistant_text(assistant_text: list[str], turn_items: list[InterviewItem]) -> None:
        text = "".join(assistant_text)
        assistant_text.clear()
        if text:
            turn_items.append(InterviewItem("assistant", text))
