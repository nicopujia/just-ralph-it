import base64
import contextlib
import logging
import mimetypes
import os
import platform
import shutil
import signal
import subprocess
import sys
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryFile
from threading import Event
from typing import Annotated, cast

import httpx
from markdownify import MarkdownConverter
from openai.types.responses import ResponseFunctionCallOutputItemListParam
from pydantic import PlainSerializer

from jri.core import ai
from jri.core.ai.agent import Agent
from jri.core.ai.tool import Invocation, tool
from jri.core.settings import Settings, read_api_key
from jri.lib import brave, files, prompt, youtube

logger = logging.getLogger(__name__)


class Explorer(Agent):
    MAX_INPUT_SIZE = 10 * 1024 * 1024

    def __init__(self, settings: Settings, directory: Path) -> None:
        self.settings = settings
        self.directory = directory
        super().__init__(
            client=settings.llm.client,
            profile=settings.agents.explorer,
            max_input_size=self.MAX_INPUT_SIZE,
            prompt=ai.prompts.read("explorer", working_directory=prompt.render(working_directory=str(directory))),
        )
        # Do not advertise a capability that this run lacks.
        # `respond` builds tool definitions from `tools` for every call.
        if not settings.brave_search.api_key:
            self.tools = [capability for capability in self.tools if capability.name != "search_web"]

    # Use only the final continuous text as the report. Text before a tool call is intermediate work.
    # Pass reasoning through, but do not add it to the report. The architect receives only the report.
    def report(
        self, query: str, depth: int = 0, cancelled: Event | None = None
    ) -> Generator["ai.ReasoningDelta | ai.ToolCallStarted | ai.ToolCallFinished", None, str]:
        output: list[str] = []
        for event in self.send_message(query, cancelled):
            match event:
                case ai.ToolCallStarted():
                    output.clear()
                    yield replace(event, depth=depth)
                case ai.ToolCallFinished():
                    yield replace(event, depth=depth)
                case ai.ReasoningDelta():
                    yield event
                case ai.TextDelta():
                    output.append(event.text)
        return "".join(output)

    @tool(
        "Explore the web with a search engine.",
        started_label="Searching the web for {query}",
        finished_label="Searched the web for {query}",
        symbol="🔎",
        replayed=False,
    )
    def search_web(self, query: str) -> str:
        results = brave.search(read_api_key(cast("str", self.settings.brave_search.api_key)), query)
        return prompt.render(search_results={result["url"]: result["title"] for result in results})

    @tool(
        "Fetch contents from a public web page given a URL.",
        started_label="Fetching {url}",
        finished_label="Fetched {url}",
        symbol="🌐",
        replayed=False,
    )
    # Quote all fetched content. A page can end with text that looks like a JRI instruction.
    def fetch_web_page(self, url: str) -> str:
        logger.debug("fetch_url url=%r", url)
        if (video_transcript := youtube.fetch_transcript_from_url(url)) is not None:
            logger.info("fetch_finished source=youtube characters=%d", len(video_transcript))
            return prompt.render(video_transcript=video_transcript)
        data = bytearray()
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=10.0) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    data.extend(chunk[: self.MAX_INPUT_SIZE - len(data)])
                    if len(data) == self.MAX_INPUT_SIZE:
                        break
        # The model can provide a URL that httpx cannot create. `InvalidURL` is not an `HTTPError`.
        except (httpx.HTTPError, httpx.InvalidURL) as error:
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
                # The row already names the URL. httpx includes the URL in a status failure.
                reason = f"{error.response.status_code} {error.response.reason_phrase}"
            else:
                logger.exception("fetch_failed url=%r", url)
                # A timeout can have no message. Use its type name so the model and reader can act on the failure.
                reason = str(error) or type(error).__name__
            raise RuntimeError(reason) from error
        response_body = data.decode(response.encoding or "utf-8", errors="replace")
        page = MarkdownConverter().convert(response_body)
        logger.info("fetch_finished status_code=%d characters=%d", response.status_code, len(page))
        logger.debug(
            "fetch_response final_url=%r headers=%r response_body=%r",
            str(response.url),
            dict(response.headers),
            response_body,
        )
        return prompt.render(web_page=page)

    @tool(
        (
            "Read text, image, and binary file(s) from the machine. "
            "For text files, line numbers are 1-based and inclusive."
        ),
        started_label="Reading {paths}",
        finished_label="Read {paths}",
        symbol="📄",
        replayed=False,
    )
    # The read row describes file paths as prose. Format only its label. Call this method with the model path list.
    def read_files(
        self,
        paths: Annotated[list[str], PlainSerializer(files.describe_paths)],
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> ResponseFunctionCallOutputItemListParam:
        logger.debug("read_paths paths=%r", paths)
        if start_line is not None and start_line < 1:
            raise ValueError(f"A range starts at line 1 at the earliest, not line {start_line}.")
        if end_line is not None and end_line < 1:
            raise ValueError(f"A range ends at line 1 at the earliest, not line {end_line}.")
        if start_line is not None and end_line is not None and start_line > end_line:
            raise ValueError(f"A range that starts at line {start_line} cannot end at line {end_line}.")
        output: ResponseFunctionCallOutputItemListParam = []
        for raw_path in paths:
            # Use absolute paths so each reported fact identifies its file without the run directory.
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = self.directory / path
            try:
                if path.stat().st_size > self.MAX_INPUT_SIZE:
                    raise RuntimeError(f"Could not read {path}: file exceeds {self.MAX_INPUT_SIZE} bytes.")
                data = path.read_bytes()
            except OSError as error:
                # A guessed path is recoverable. Log it as a warning.
                # An error traceback for each miss hides important failures.
                logger.warning("read_failed path=%r reason=%s", path, error.strerror)
                raise RuntimeError(f"Could not read {path}: {error.strerror}") from error

            # The API joins this header with the next body item. Quote both so file content cannot forge the header.
            output.append({"type": "input_text", "text": prompt.render(file=str(path))})
            media_type = mimetypes.guess_type(path.name)[0]
            if media_type and media_type.startswith("image/"):
                encoded = base64.b64encode(data).decode("ascii")
                output.append({"type": "input_image", "image_url": f"data:{media_type};base64,{encoded}"})
                continue

            try:
                text = data.decode()
            except UnicodeDecodeError:
                output.append({
                    "type": "input_file",
                    "filename": path.name,
                    "file_data": base64.b64encode(data).decode("ascii"),
                })
                continue

            if start_line is not None or end_line is not None:
                lines = text.splitlines(keepends=True)
                # A slice after the last line returns no text and looks like an empty file.
                if start_line is not None and start_line > len(lines):
                    raise RuntimeError(
                        f"Could not read {path}: it ends at line {len(lines)}, before line {start_line}."
                    )
                text = "".join(lines[(start_line or 1) - 1 : end_line])
            output.append({"type": "input_text", "text": prompt.render(content=text)})
        logger.info("read_finished files=%d output_items=%d", len(paths), len(output))
        return output

    @tool(
        f"Run a shell command on this machine (OS: {platform.system()}).",
        started_label="Running {command}",
        finished_label="Ran {command}",
        symbol="💻",
    )
    def run_shell(self, command: str) -> str:
        logger.debug("shell_command command=%r", command)
        # Windows has no `/bin/sh` and uses command-line rules that `list2cmdline` does not create.
        # Give it the command line. `/d` skips the registry command. `/s` removes outer quotes and uses the rest.
        # On other platforms, a login shell gives the command the user's terminal PATH.
        arguments = (
            f'"{os.environ.get("COMSPEC", "cmd.exe")}" /d /s /c "{command}"'
            if sys.platform == "win32"
            else ["/bin/sh", "-lc", command]
        )
        with TemporaryFile("w+", encoding="utf-8", errors="replace") as output_file:
            process = subprocess.Popen(
                arguments,
                cwd=self.directory,
                stdout=output_file,
                stderr=subprocess.STDOUT,
                # On POSIX, start a separate session. The command and its processes are then one group to stop.
                start_new_session=True,
            )
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                _stop_process_tree(process.pid)
                process.wait()
                output_file.seek(0)
                output = output_file.read(Invocation.MAX_OUTPUT_LENGTH)
                logger.exception("shell_timed_out command=%r output=%r", command, output)
                raise RuntimeError(f"Command timed out after 30 seconds:\n{output}".rstrip()) from None
            output_file.seek(0)
            output = output_file.read(Invocation.MAX_OUTPUT_LENGTH)
        _stop_process_tree(process.pid)
        if process.returncode != 0:
            logger.error("shell_failed return_code=%d output_characters=%d", process.returncode, len(output))
            raise RuntimeError(f"Command exited with status {process.returncode}:\n{output}".rstrip())
        logger.info("shell_finished return_code=%d output_characters=%d", process.returncode, len(output))
        return output


def _stop_process_tree(pid: int) -> None:
    if sys.platform != "win32":
        # A session leader PID is also its process-group ID. It identifies child processes after the shell exits.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
        return
    # Windows cannot signal a process group. `taskkill` starts at the shell and misses children after the shell exits.
    executable = shutil.which("taskkill")
    if executable is not None:
        subprocess.run([executable, "/F", "/T", "/PID", str(pid)], check=False, capture_output=True)
