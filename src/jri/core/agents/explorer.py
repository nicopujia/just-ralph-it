import platform
import subprocess
from typing import override

import httpx
from markdownify import MarkdownConverter
from openai.types.responses import ResponseInputItemParam

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

    @tool("Explore the web with a search engine.", running_label="Searching web", finished_label="Searched web")
    def web_search(self, query: str) -> str:
        """Search the web for links relevant to the query.

        Returns:
            Markdown links, or an unavailable message.
        """
        if not self.settings.brave_api_key:
            return "Web search not available."
        client = brave.LLMContext(self.settings.brave_api_key)
        results = client.search(query)
        return "\n".join(f"- [{r.title}]({r.url})" for r in results.generic)

    @staticmethod
    @tool(
        "Fetch contents from a public web page given a URL.",
        running_label="Fetching page",
        finished_label="Fetched page",
    )
    def web_fetch(url: str) -> str:
        """Fetch readable page or YouTube transcript content.

        Returns:
            Page markdown, transcript text, or an error message.
        """
        try:
            video_transcript = youtube.fetch_transcript_from_url(url)
        except youtube.InvalidUrlError:
            return "Web fetch failed: invalid YouTube URL."
        except youtube.TranscriptError:
            return "Web fetch failed: could not retrieve YouTube transcript."

        if video_transcript is not None:
            return video_transcript

        try:
            return MarkdownConverter().convert(httpx.get(url, follow_redirects=True, timeout=10.0).text)
        except httpx.HTTPError as error:
            return f"Web fetch failed: {error}"

    @staticmethod
    @tool(
        f"Run a shell command on this {platform.system()} machine.",
        running_label="Running command",
        finished_label="Ran command",
    )
    def shell(cmd: str) -> str:
        """Run a read-only shell exploration command.

        Returns:
            Captured standard output from the command.
        """
        return subprocess.run(["/bin/sh", "-lc", cmd], check=False, capture_output=True, text=True).stdout

    @override
    def after_tool_call(self, _tool_name: str, _turn_items: list[ResponseInputItemParam]) -> None:
        """Explorer keeps its context unchanged after tool calls."""
