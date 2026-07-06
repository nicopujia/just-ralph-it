import json
import textwrap
from collections.abc import Generator
from pathlib import Path

from openai import BaseModel as OpenAIModel

from .agents import Interviewer
from .agents.shared import ChatEvent
from .settings import Settings, get_settings


class Service:
    def __init__(self, settings: Settings | None = None) -> None:
        """Load settings and set base directory up.

        Directory structure:
        ```
            $CWD/.jri/
                .gitignore
                interview.json
        ```
        """
        self.settings: Settings = settings or get_settings()
        self.interviewer: Interviewer = Interviewer(self.settings)
        self.base_dir: Path = Path(self.settings.cwd / ".jri")
        self.interview_file: Path = self.base_dir / "interview.json"
        self.gitignore_file: Path = self.base_dir / ".gitignore"
        self.base_dir.mkdir(exist_ok=True, parents=True)
        _chars_written = self.gitignore_file.write_text(
            textwrap.dedent(
                """
                interview.json
                """,
            ).strip(),
        )

    def chat(self, message: str) -> Generator[ChatEvent]:
        """Send a message and persist the full interview context.

        Yields:
            Streamed chat events from the interviewer.
        """
        yield from self.interviewer.send_message(message)
        interview_json = [
            item.model_dump(mode="json")
            if isinstance(item, OpenAIModel)
            else item
            for item in self.interviewer.ctx
        ]
        interview_json_str = f"{json.dumps(interview_json, indent=2)}\n"
        _chars_written = self.interview_file.write_text(interview_json_str)
