import json
import shutil
import textwrap
from collections.abc import Generator
from typing import TYPE_CHECKING, Literal, NamedTuple, cast

from openai import BaseModel as OpenAIModel

from .agents import Interviewer
from .agents.shared import ChatEvent
from .settings import Settings

if TYPE_CHECKING:
    from pathlib import Path

    from openai.types.responses import ResponseInputItemParam


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
        self.interviewer: Interviewer = Interviewer(settings)

        self.base_dir: Path = settings.cwd / ".jri"
        self.gitignore_file: Path = self.base_dir / ".gitignore"
        self.interview_file: Path = self.base_dir / "interview.json"

        if settings.force:
            shutil.rmtree(self.base_dir)

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

    def restore(self) -> list[InterviewItem] | None:
        """Restore interview session if present.

        Returns:
            List of interview items if present, or None otherwise.
        """
        try:
            saved_items = cast(
                "object",
                json.loads(self.interview_file.read_text()),
            )
        except (OSError, ValueError):
            return None

        if not isinstance(saved_items, list):
            return None

        saved_items = cast("list[object]", saved_items)
        if not all(isinstance(item, dict) for item in saved_items):
            return None

        ctx: list[dict[str, object]] = [
            dict(cast("dict[str, object]", item)) for item in saved_items
        ]
        ctx = [
            {"role": "system", "content": self.interviewer.sys_prompt},
            *ctx[int(bool(ctx and ctx[0].get("role") == "system")) :],
        ]

        self.interviewer.ctx = cast("list[ResponseInputItemParam]", ctx)
        restored_items: list[InterviewItem] = []
        for item in ctx:
            if item.get("type") == "function_call":
                name = item.get("name")
                text = name if isinstance(name, str) else "tool"
                restored_items.append(InterviewItem("tool", text))
                continue

            role, content = item.get("role"), item.get("content")
            if role not in {"user", "assistant"}:
                continue

            role = cast("Literal['user', 'assistant']", role)
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "")
                    for part in cast("list[dict[str, str]]", content)
                    if part.get("type") == "output_text"
                )

            if isinstance(content, str) and content:
                restored_items.append(InterviewItem(role, content))

        return restored_items
