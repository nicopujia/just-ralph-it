import base64
import contextlib
import logging
import mimetypes
import os
import platform
import signal
import subprocess
from pathlib import Path
from tempfile import TemporaryFile

import httpx
from markdownify import MarkdownConverter
from openai.types.responses import ResponseFunctionCallOutputItemListParam

from jri.core.settings import Settings, read_api_key
from jri.lib import brave, youtube

from .base import MAX_OUTPUT_LENGTH, Agent, tool

logger = logging.getLogger(__name__)


class Explorer(Agent):
    MAX_INPUT_SIZE = 10 * 1024 * 1024

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        agent = settings.agents.explorer
        super().__init__(
            client=settings.llm.client,
            model=agent.model,
            temperature=agent.temperature,
            reasoning_effort=agent.reasoning_effort,
            max_input_size=self.MAX_INPUT_SIZE,
            sys_prompt=f"""
                Role: Explorer.

                Goal: Gather relevant context based on the given query.

                Working directory: {self.settings.cwd}

                Output:
                    - A dense, concise, and purely factual report based exclusively on data from tool outputs.
                    - Attribute each fact to the file path, command, or URL it came from.

                Tools:
                    - Prefer `web_fetch` for URLs and `read_files` for file contents, over `shell`.
                    - Once `web_search` reports being unavailable, rely on the other tools for the rest of the run.

                Constraints:
                    - Use `shell` only to observe: treat this machine as read-only.
                    - Bound every shell command to at most 30 seconds, and stop each process it starts before
                    returning.
                    - State any ambiguity explicitly when the information you need is missing.
            """,
        )

    @tool(
        "Explore the web with a search engine.",
        started_label="Searching the web for {query}",
        finished_label="Searched the web for {query}",
        symbol="🔎",
        read_only=True,
    )
    def web_search(self, query: str) -> str:
        logger.debug("search_query query=%r", query)
        if not self.settings.brave_search.api_key:
            logger.info("search_finished available=False")
            return "Web search not available."
        results = brave.search(read_api_key(self.settings.brave_search.api_key), query)
        output = "\n".join(f"- [{result['title']}]({result['url']})" for result in results)
        logger.info("search_finished results=%d", len(results))
        return output

    @tool(
        "Fetch contents from a public web page given a URL.",
        started_label="Fetching {url}",
        finished_label="Fetched {url}",
        symbol="🌐",
        read_only=True,
    )
    def web_fetch(self, url: str) -> str:
        logger.debug("fetch_url url=%r", url)
        if (video_transcript := youtube.fetch_transcript_from_url(url)) is not None:
            logger.info("fetch_finished source=youtube characters=%d", len(video_transcript))
            return video_transcript
        limit = self.runner.max_input_size
        data = bytearray()
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=10.0) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    data.extend(chunk if limit is None else chunk[: limit - len(data)])
                    if limit is not None and len(data) == limit:
                        break
        except httpx.HTTPError as error:
            if isinstance(error, httpx.HTTPStatusError):
                logger.debug(
                    "fetch_error_response final_url=%r headers=%r",
                    str(error.response.url),
                    dict(error.response.headers),
                )
                logger.exception(
                    "fetch_failed url=%r final_url=%r status_code=%r",
                    url,
                    str(error.response.url),
                    error.response.status_code,
                )
            else:
                logger.exception("fetch_failed url=%r", url)
            raise RuntimeError(f"Could not fetch {url}: {error}") from error
        response_body = data.decode(response.encoding or "utf-8", errors="replace")
        output = MarkdownConverter().convert(response_body)
        logger.info("fetch_finished status_code=%d characters=%d", response.status_code, len(output))
        logger.debug(
            "fetch_response final_url=%r headers=%r response_body=%r",
            str(response.url),
            dict(response.headers),
            response_body,
        )
        return output

    @tool(
        (
            "Read text, image, and binary file(s) from the machine. "
            "For text files, line numbers are 1-based and inclusive."
        ),
        started_label="Reading {paths}",
        finished_label="Read {paths}",
        symbol="📄",
        read_only=True,
    )
    def read_files(
        self, paths: list[str], start_line: int | None = None, end_line: int | None = None
    ) -> ResponseFunctionCallOutputItemListParam:
        logger.debug("read_paths paths=%r", paths)
        limit = self.runner.max_input_size
        output: ResponseFunctionCallOutputItemListParam = []
        for raw_path in paths:
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = self.settings.cwd / path
            try:
                if limit is not None and path.stat().st_size > limit:
                    raise RuntimeError(f"Could not read {path}: file exceeds {limit} bytes.")
                data = path.read_bytes()
            except OSError as error:
                logger.exception("read_failed path=%r", path)
                raise RuntimeError(f"Could not read {path}: {error.strerror}") from error

            output.append({"type": "input_text", "text": f"File: {path}"})
            media_type = mimetypes.guess_type(path.name)[0]
            if media_type and media_type.startswith("image/"):
                encoded = base64.b64encode(data).decode("ascii")
                output.append({"type": "input_image", "image_url": f"data:{media_type};base64,{encoded}"})
                continue

            try:
                text = data.decode()
                if start_line is not None or end_line is not None:
                    text = "".join(text.splitlines(keepends=True)[(start_line or 1) - 1 : end_line])
                output.append({"type": "input_text", "text": text})
            except UnicodeDecodeError:
                output.append({
                    "type": "input_file",
                    "filename": path.name,
                    "file_data": base64.b64encode(data).decode("ascii"),
                })
        logger.info("read_finished files=%d output_items=%d", len(paths), len(output))
        return output

    @tool(
        f"Run a shell command on this machine (OS: {platform.system()}).",
        started_label="Running {cmd}",
        finished_label="Ran {cmd}",
        symbol="💻",
    )
    def shell(self, cmd: str) -> str:
        logger.debug("shell_command command=%r", cmd)
        with TemporaryFile("w+", encoding="utf-8", errors="replace") as output_file:
            process = subprocess.Popen(
                ["/bin/sh", "-lc", cmd],
                cwd=self.settings.cwd,
                stdout=output_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                output_file.seek(0)
                output = output_file.read(MAX_OUTPUT_LENGTH)
                logger.exception("shell_timed_out command=%r output=%r", cmd, output)
                raise RuntimeError(f"Command timed out after 30 seconds:\n{output}".rstrip()) from None
            output_file.seek(0)
            output = output_file.read(MAX_OUTPUT_LENGTH)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        if process.returncode != 0:
            logger.error("shell_failed return_code=%d output_characters=%d", process.returncode, len(output))
            raise RuntimeError(f"Command exited with status {process.returncode}:\n{output}".rstrip())
        logger.info("shell_finished return_code=%d output_characters=%d", process.returncode, len(output))
        return output
