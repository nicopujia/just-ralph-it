import json
import shutil
from collections.abc import Generator

from .agents import Interviewer
from .agents.shared import ChatEvent, TextDelta, ToolCallStarted
from .notes import Notes
from .notes.models import InterviewItem
from .settings import Settings


class Service:
    def __init__(self, settings: Settings) -> None:
        """Load settings and set base directory up.

        Directory structure:
        ```
            $CWD/.jri/
                .gitignore
                logs/
                    interview.json
                notes.yaml
                state.json
        ```
        """
        self.base_dir = settings.cwd / ".jri"
        self.gitignore_file = self.base_dir / ".gitignore"
        self.logs_dir = self.base_dir / "logs"
        self.interview_log_file = self.logs_dir / "interview.json"
        self.notes_file = self.base_dir / "notes.yaml"
        self.state_file = self.base_dir / "state.json"

        if settings.force:
            if self.base_dir.is_dir() and not self.base_dir.is_symlink():
                shutil.rmtree(self.base_dir)
            else:
                self.base_dir.unlink(missing_ok=True)

        self.base_dir.mkdir(exist_ok=True, parents=True)

        self.gitignore_file.write_text("state.json\nlogs/\n")
        self.notes = Notes(self.notes_file, self.state_file)
        self.interviewer = Interviewer(settings, self.notes)
        self.interview_items = self.notes.state.interview.items

    def chat(self, message: str) -> Generator[ChatEvent]:
        """Send a message and persist the visible interview transcript.

        Yields:
            Streamed chat events from the interviewer.
        """
        turn_items = [InterviewItem(type="user", text=message)]
        assistant_text: list[str] = []

        for chat_event in self.interviewer.send_message(message):
            if isinstance(chat_event, TextDelta):
                assistant_text.append(chat_event.text)
            elif isinstance(chat_event, ToolCallStarted):
                text = "".join(assistant_text)
                assistant_text.clear()
                if text:
                    turn_items.append(InterviewItem(type="assistant", text=text))
                turn_items.append(InterviewItem(type="tool", text=chat_event.finished_label))
            yield chat_event

        if text := "".join(assistant_text):
            turn_items.append(InterviewItem(type="assistant", text=text))
        self.interview_items.extend(turn_items)
        self._save_interview()

    def restore(self) -> list[InterviewItem]:
        """Restore interview session if present.

        Returns:
            List of interview items if present, which may be empty.
        """
        if self.notes.state.interview.context:
            self.interviewer.restore_context(self.notes.state.interview.context)
        else:
            self.interviewer.rebuild_context()
        return list(self.interview_items)

    def _save_interview(self) -> None:
        payload = [item.model_dump(mode="json") for item in self.interview_items]
        self.notes.state.interview.context = self.interviewer.dump_context()
        self.notes.save_state()
        self.logs_dir.mkdir(exist_ok=True, parents=True)
        self.interview_log_file.write_text(f"{json.dumps(payload, indent=2)}\n", encoding="utf-8")
