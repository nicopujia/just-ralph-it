import platform
import subprocess

import httpx
from markdownify import MarkdownConverter

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
    @tool(f"Run a shell command on this {platform.system()} machine.")
    def shell(cmd: str) -> str:
        return subprocess.run(["/bin/sh", "-lc", cmd], check=False, capture_output=True, text=True).stdout
