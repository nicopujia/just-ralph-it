import json
import shutil
from collections.abc import Generator
from typing import Literal, NamedTuple

from openai import BaseModel as OpenAIModel

from .agents import ChatEvent, Interviewer
from .settings import Settings


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
                interview.json
        ```
        """
        self.interviewer = Interviewer(settings)

        self.base_dir = settings.cwd / ".jri"
        self.gitignore_file = self.base_dir / ".gitignore"
        self.interview_file = self.base_dir / "interview.json"

        if settings.force:
            shutil.rmtree(self.base_dir)

        self.base_dir.mkdir(exist_ok=True, parents=True)
        self.gitignore_file.write_text("interview.json")

    def chat(self, message: str) -> Generator[ChatEvent]:
        """Send a message and persist the full interview context.

        Yields:
            Streamed chat events from the interviewer.
        """
        yield from self.interviewer.send_message(message)
        interview_json = [
            (item.model_dump(mode="json") if isinstance(item, OpenAIModel) else item) for item in self.interviewer.ctx
        ]
        interview_json_str = f"{json.dumps(interview_json, indent=2)}\n"
        self.interview_file.write_text(interview_json_str)

    def restore(self) -> list[InterviewItem]:
        """Restore interview session if present.

        Returns:
            List of interview items if present, which may be empty.
        """
        if not self.interview_file.exists():
            return []
        self.interviewer.ctx = json.loads(self.interview_file.read_text())
        items: list[InterviewItem] = []
        for item in self.interviewer.ctx[1:]:  # Skip system prompt
            item_type = item.get("type")
            if item_type == "function_call":
                items.append(InterviewItem("tool", item["name"]))
                continue
            if item_type not in {None, "message"}:
                continue
            content = item["content"]
            if isinstance(content, list):
                content = "".join(part["text"] for part in content if part["type"] == "output_text")
            if content:
                items.append(InterviewItem(item["role"], content))
        return items
