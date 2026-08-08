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
from jri.core.settings import Settings, read_api_key
from jri.lib import brave, files, prompt, youtube

from .base import Agent, Invocation, tool

logger = logging.getLogger(__name__)


class Explorer(Agent):
    MAX_INPUT_SIZE = 10 * 1024 * 1024

    def __init__(self, settings: Settings, directory: Path) -> None:
        self.settings = settings
        self.directory = directory
        profile = settings.agents.explorer
        super().__init__(
            client=settings.llm.client,
            model=profile.model,
            temperature=profile.temperature,
            reasoning_effort=profile.reasoning_effort,
            max_input_size=self.MAX_INPUT_SIZE,
            prompt=(
                "Role: Explorer.\n"
                "\n"
                "Goal: Gather relevant context based on the given query.\n"
                "\n"
                # The filesystem named this, not JRI: a line break is a
                # legal POSIX filename character, so as prose a
                # directory whose name carries one writes further
                # sections of these instructions, at the depth JRI's
                # own sit at and ahead of them.
                f"{prompt.render(working_directory=str(directory))}\n"
                "\n"
                "Output:\n"
                "    - A dense, concise, and purely factual report based exclusively on data from tool outputs.\n"
                "    - Attribute each fact to the file path, command, or URL it came from.\n"
                "\n"
                "Tools:\n"
                "    - Prefer `fetch_web_page` for URLs and `read_files` for file contents, over `run_shell`.\n"
                "\n"
                "Constraints:\n"
                "    - Use `run_shell` only to observe: treat this machine as read-only.\n"
                "    - Bound every shell command to at most 30 seconds, and stop each process it starts before\n"
                "    returning.\n"
                "    - State any ambiguity explicitly when the information you need is missing."
            ),
        )
        # A capability this run does not have is absent, not advertised
        # and then refused: `respond` rebuilds the definitions it offers
        # from `tools` on every call.
        if not settings.brave_search.api_key:
            self.tools = [capability for capability in self.tools if capability.name != "search_web"]

    # Only the last uninterrupted stretch of text is the report: a tool
    # call means the run was still gathering, so whatever it had said
    # before that is working-out rather than conclusion.
    def report(
        self, query: str, depth: int = 0, cancelled: Event | None = None
    ) -> Generator["ai.ToolCallStarted | ai.ToolCallFinished", None, str]:
        output: list[str] = []
        for event in self.send_message(query, cancelled):
            match event:
                case ai.ToolCallStarted():
                    output.clear()
                    yield replace(event, depth=depth)
                case ai.ToolCallFinished():
                    yield replace(event, depth=depth)
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
        logger.debug("search_query query=%r", query)
        results = brave.search(read_api_key(cast("str", self.settings.brave_search.api_key)), query)
        output = prompt.render(search_results={result["url"]: result["title"] for result in results})
        logger.info("search_finished results=%d", len(results))
        return output

    @tool(
        "Fetch contents from a public web page given a URL.",
        started_label="Fetching {url}",
        finished_label="Fetched {url}",
        symbol="🌐",
        replayed=False,
    )
    # Whatever comes back is quoted. A fetch is the one output long
    # enough for JRI to end with a sentence of its own, and a page can
    # be written to end with that very sentence: unquoted, the two read
    # alike, and the fetch that arrived whole was the only thing making
    # a page safe to hand over with no structure of JRI's around it.
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
        # A model can invent a URL httpx refuses to even build, and
        # that refusal is no HTTPError.
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
                # The row this reason lands on already names the URL,
                # and httpx words a status failure with the URL in it.
                reason = f"{error.response.status_code} {error.response.reason_phrase}"
            else:
                logger.exception("fetch_failed url=%r", url)
                # A timeout reaches here saying nothing about itself,
                # and a failure that asserts nothing is one neither the
                # model nor the reader can act on.
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
    # The row a read opens is a sentence about files, so the paths
    # reach the label as prose. Only the label dumps the arguments:
    # the call itself is made with the list the model sent.
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
            # Absolute throughout, so every path the report attributes a
            # fact to names the file without the reader knowing the
            # directory the run happened in.
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = self.directory / path
            try:
                if path.stat().st_size > self.MAX_INPUT_SIZE:
                    raise RuntimeError(f"Could not read {path}: file exceeds {self.MAX_INPUT_SIZE} bytes.")
                data = path.read_bytes()
            except OSError as error:
                # A path the model guessed at is a miss it recovers from
                # by reading somewhere else, so it is reported at the
                # weight of the event: an ERROR with a traceback per
                # missed guess buries the failures worth finding.
                logger.warning("read_failed path=%r reason=%s", path, error.strerror)
                raise RuntimeError(f"Could not read {path}: {error.strerror}") from error

            # The body is a sibling item the API concatenates onto this
            # one, so quoting the header alone would leave a file whose
            # contents hold a quoted header able to forge both.
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
                # A slice that begins past the last line answers with
                # nothing, which reads as a file holding nothing.
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
        # Windows has no `/bin/sh`, and its interpreter reads a command
        # line by rules `list2cmdline` does not write, so it is handed
        # the line itself: `/d` drops whatever the registry would run
        # first, and `/s` makes it strip the outer quotes and take the
        # rest verbatim. Elsewhere a login shell gives the command the
        # PATH the person's own terminal would.
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
                # POSIX only, and ignored elsewhere: the command leads a
                # session of its own, so everything it starts is one
                # group to stop.
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
        # The pid of a session leader is its process group's, and the
        # group outlives the leader for as long as anything it started
        # runs, so a reaped shell still names what it left behind.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
        return
    # Windows has no group to signal: `taskkill` walks the tree from
    # the shell down, and a shell that has already exited leaves it
    # nothing to walk, so a background process outlives the call.
    executable = shutil.which("taskkill")
    if executable is not None:
        subprocess.run([executable, "/F", "/T", "/PID", str(pid)], check=False, capture_output=True)
