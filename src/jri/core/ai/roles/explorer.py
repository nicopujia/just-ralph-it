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
from typing import Annotated, ClassVar, cast, override

import httpx
from markdownify import MarkdownConverter
from openai.types.responses import ResponseFunctionCallOutputItemListParam, ResponseInputParam
from pydantic import BaseModel, PlainSerializer

from jri.core import ai
from jri.core.ai.agent import Agent
from jri.core.ai.tool import Invocation, Tool, tool
from jri.core.settings import Settings, read_api_key
from jri.lib import brave, files, prompt, youtube
from jri.lib.context import estimate_tokens, measure_request
from jri.lib.models_dot_dev import get_input_room

logger = logging.getLogger(__name__)


# What one segment of an exploration answers with. The report is the whole of the findings, the summary stands for
# it where the whole does not fit, and the remaining work is what a further segment takes up.
class Exploration(BaseModel):
    report: str
    summary: str
    remaining: str


class Explorer(Agent):
    MAX_INPUT_SIZE = 10 * 1024 * 1024
    # An exploration runs at most this many segments. Each one costs a full request, and a query that ten of them
    # do not answer is one that no further segment answers either.
    MAX_SEGMENTS: ClassVar[int] = 10
    # End a segment past this share of the room the model reads. The rest of that room holds the report that the
    # segment writes, and the reasoning the model keeps while it writes it.
    INPUT_SHARE: ClassVar[float] = 0.8
    # A segment ends where its request runs out of room. Record that, because a model with no tool reads it as a
    # tool it lost, not as a segment that ends.
    INPUT_LIMIT_RECORD: ClassVar[str] = (
        "This request is at its size limit. No more tool output fits in this segment of the exploration."
    )
    # The last segment has no segment after it, so a record of its own says that no room follows this one either.
    FINAL_LIMIT_RECORD: ClassVar[str] = (
        "This request is at its size limit, and this is the last segment of the exploration. "
        "No more tool output fits in it, and no segment follows it."
    )
    FINAL_SEGMENT_PROMPT = ai.prompts.read("explorer_final_segment")

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
        # Each round builds its tool definitions from `tools`.
        if not settings.brave_search.api_key:
            self.tools = [capability for capability in self.tools if capability.name != "search_web"]
        self.at_input_limit = False
        self.final_segment = False

    # Measure the request that the round about to start sends. Past the mark, record that the segment stands at
    # its limit, so that round reports the findings it has instead of gathering more.
    @override
    def get_context(self) -> ResponseInputParam:
        if not self.at_input_limit:
            size = measure_request(self.history, [item.definition for item in self.get_tools()])
            room = get_input_room(self.profile.model, self.FALLBACK_INPUT_ROOM)
            if estimate_tokens(size) > room * self.INPUT_SHARE:
                self.at_input_limit = True
                record = self.FINAL_LIMIT_RECORD if self.final_segment else self.INPUT_LIMIT_RECORD
                self.history.append({"role": "system", "content": record})
                logger.info("exploration_limit_reached tokens=%d room=%d", estimate_tokens(size), room)
        return self.history

    # A segment at its limit has nothing left to gather. Take its tools away, so the rounds it has left write the
    # report rather than fill the request further.
    @override
    def get_tools(self) -> list[Tool]:
        return [] if self.at_input_limit else self.tools

    # One exploration runs in segments, and each segment is a call of its own that starts from the query, the
    # findings so far, and what is left. They share one round budget, because `parse` never refills it.
    def report(
        self, query: str, depth: int = 0, cancelled: Event | None = None
    ) -> Generator["ai.ReasoningDelta | ai.ToolCallStarted | ai.ToolCallFinished", None, Exploration | None]:
        exploration = None
        for segment in range(1, self.MAX_SEGMENTS + 1):
            self.at_input_limit = False
            self.final_segment = segment == self.MAX_SEGMENTS
            # The first segment carries the query alone, which the caller wrote for a model to read whole.
            message = query if exploration is None else self._render(query, exploration)
            exploration = yield from _stamp_rows(self.parse(message, Exploration, cancelled), depth)
            if exploration is None or not exploration.remaining.strip():
                return exploration
        return exploration

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

    def _render(self, query: str, exploration: Exploration) -> str:
        return ai.prompts.read(
            "explorer_segment",
            final_rule=f"\n{self.FINAL_SEGMENT_PROMPT}\n" if self.final_segment else "",
            context=prompt.render(
                exploration_query=query, findings_so_far=exploration.report, remaining_work=exploration.remaining
            ),
        )


# `yield from` passes on each event as it is, and every row of a segment carries the depth of the caller of the
# exploration. Drain the segment here, stamp each row it opens, and give the result back to the caller.
def _stamp_rows(
    segment: Generator["ai.ReasoningDelta | ai.ToolCallStarted | ai.ToolCallFinished", None, Exploration | None],
    depth: int,
) -> Generator["ai.ReasoningDelta | ai.ToolCallStarted | ai.ToolCallFinished", None, Exploration | None]:
    while True:
        try:
            event = next(segment)
        except StopIteration as stop:
            return cast("Exploration | None", stop.value)
        yield event if isinstance(event, ai.ReasoningDelta) else replace(event, depth=depth)


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
