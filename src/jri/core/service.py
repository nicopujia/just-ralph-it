import json
import shutil
from collections.abc import Generator
from typing import Literal, NamedTuple

from openai import BaseModel as OpenAIModel

from .agents import Interviewer
from .agents.shared import ChatEvent
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
        try:
            self.interviewer.ctx = json.loads(self.interview_file.read_text())
        except (OSError, ValueError):
            return []
        items: list[InterviewItem] = []
        for item in self.interviewer.ctx[1:]:
            if item.get("type") == "function_call":
                name = item.get("name")
                text = name if isinstance(name, str) else "tool"
                items.append(InterviewItem("tool", text))
                continue
            if (content := item.get("content")) and (role := item.get("role")):
                content = "".join(
                    text
                    for part in content
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "output_text"
                        and isinstance((text := part.get("text")), str)
                    )
                )
                if role == "user" or role == "assistant":  # noqa: PLR1714
                    items.append(InterviewItem(role, content))
        return items
