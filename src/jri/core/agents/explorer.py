import base64
import mimetypes
import platform
import subprocess
from pathlib import Path

import httpx
from markdownify import MarkdownConverter
from openai.types.responses import ResponseFunctionCallOutputItemListParam

from jri.core.settings import Settings
from jri.lib import brave, youtube

from .shared import Agent, tool


class Explorer(Agent):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        super().__init__(
            client=self.settings.llm_client,
            model=self.settings.explorer_model,
            sys_prompt="""
                Given a query, use your tools to gather relevant context
                and respond with a dense, concise, and purely factual report
                based exclusively on tool outputs.

                **CRITICAL**: NEVER use `shell` tool to mutate machine state.
                You may only use it for exploration purposes.
            """,
        )

    @tool("Explore the web with a search engine.")
    def web_search(self, query: str) -> str:
        if not self.settings.brave_api_key:
            return "Web search not available."
        results = brave.search(self.settings.brave_api_key, query)
        return "\n".join(f"- [{result.title}]({result.url})" for result in results)

    @staticmethod
    @tool("Fetch contents from a public web page given a URL.")
    def web_fetch(url: str) -> str:
        if (video_transcript := youtube.fetch_transcript_from_url(url)) is not None:
            return video_transcript
        return MarkdownConverter().convert(httpx.get(url, follow_redirects=True, timeout=10.0).text)

    @staticmethod
    @tool("Read text, image, and binary file(s) from the machine given their paths.")
    def read_files(paths: list[str]) -> ResponseFunctionCallOutputItemListParam:
        output: ResponseFunctionCallOutputItemListParam = []
        for raw_path in paths:
            path = Path(raw_path).expanduser()
            try:
                data = path.read_bytes()
            except OSError as error:
                raise RuntimeError(f"Could not read {path}: {error.strerror}") from error

            output.append({"type": "input_text", "text": f"File: {path}"})
            media_type = mimetypes.guess_type(path.name)[0]
            if media_type and media_type.startswith("image/"):
                encoded = base64.b64encode(data).decode("ascii")
                output.append({"type": "input_image", "image_url": f"data:{media_type};base64,{encoded}"})
                continue

            try:
                output.append({"type": "input_text", "text": data.decode()})
            except UnicodeDecodeError:
                output.append({
                    "type": "input_file",
                    "filename": path.name,
                    "file_data": base64.b64encode(data).decode("ascii"),
                })
        return output

    @staticmethod
    @tool(f"Run a shell command on this {platform.system()} machine.")
    def shell(cmd: str) -> str:
        return subprocess.run(["/bin/sh", "-lc", cmd], check=False, capture_output=True, text=True).stdout
