import platform
import subprocess

import httpx
from markdownify import MarkdownConverter

from jri.core.settings import Settings
from jri.lib.brave import BraveLLMContext

from .shared import Agent, tool


class Explorer(Agent):
    def __init__(self, settings: Settings) -> None:
        self.settings: Settings = settings
        super().__init__(
            client=self.settings.llm_client,
            model=self.settings.explorer_model,
            sys_prompt="""
                Given a query, use your tools to gather relevant context
                and respond with a dense, concise, and purely factual report
                based exclusively on tool outputs.
            """,
        )

    @tool("Explore the web with a search engine.")
    def web_search(self, query: str) -> str:
        if not self.settings.brave_api_key:
            return "Web search not available."
        client = BraveLLMContext(self.settings.brave_api_key)
        results = client.search(query)
        return "\n".join([f"- [{r.title}]({r.url})" for r in results.generic])

    @classmethod
    @tool("Fetch contents from a public web page given a URL.")
    def web_fetch(cls, url: str) -> str:
        try:
            response = httpx.get(url, follow_redirects=True, timeout=10.0)
        except httpx.HTTPError as error:
            return f"Web fetch failed: {error}"
        return MarkdownConverter().convert(response.text)

    @classmethod
    @tool(f"Run a shell command on this {platform.system()} machine.")
    def shell(cls, cmd: str) -> str:
        if cmd.startswith("rm"):
            return "Data deletion is forbidden."
        proc = subprocess.run(
            ["/bin/sh", "-lc", cmd],
            check=False,
            capture_output=True,
            text=True,
        )
        return proc.stdout
