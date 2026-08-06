import base64
import json
import logging
import os
import time
from pathlib import Path

import httpx
import pytest

from jri.core.ai import Explorer, Invocation, Tool
from jri.lib import brave, youtube
from tests.doubles.brave import RESULTS, FakeProvider, respond
from tests.doubles.openai import FakeClient
from tests.doubles.process import serve_timeout
from tests.doubles.settings import build_settings
from tests.doubles.web import serve_chunks, serve_pages
from tests.doubles.youtube import TRANSCRIPT, FakeApi

KILOBYTE = 1024
PNG_HEADER = b"\x89PNG\r\n\x1a\n"
UNDECODABLE = b"\xff\xfe\x00binary"


def build_explorer(directory: Path | None = None) -> Explorer:
    return Explorer(build_settings(FakeClient([])), directory or Path.cwd())


def find_read_files(explorer: Explorer) -> Tool:
    return next(capability for capability in explorer.tools if capability.name == "read_files")


def test_reads_a_selected_range_of_lines(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("one\ntwo\nthree\nfour\n")

    result = build_explorer().read_files([path.name], start_line=2, end_line=3)

    assert result[0] == {"type": "input_text", "text": f"File:\n```\n{path}\n```"}
    assert result[1] == {"type": "input_text", "text": "Content:\n```\ntwo\nthree\n\n```"}


def test_reads_to_the_end_of_a_file_a_range_overshoots(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("one\ntwo\n")

    result = build_explorer().read_files([path.name], start_line=2, end_line=99)

    assert result[1] == {"type": "input_text", "text": "Content:\n```\ntwo\n\n```"}


@pytest.mark.parametrize(
    ("start_line", "end_line", "message"),
    [
        (0, 3, "starts at line 1 at the earliest, not line 0"),
        (-2, 3, "starts at line 1 at the earliest, not line -2"),
        (1, 0, "ends at line 1 at the earliest, not line 0"),
        (1, -3, "ends at line 1 at the earliest, not line -3"),
        (3, 2, "starts at line 3 cannot end at line 2"),
    ],
    ids=["zero-start", "negative-start", "zero-end", "negative-end", "end-before-start"],
)
def test_refuses_a_line_range_that_covers_nothing(tmp_path: Path, start_line: int, end_line: int, message: str) -> None:
    path = tmp_path / "example.txt"
    path.write_text("one\ntwo\nthree\n")

    with pytest.raises(ValueError, match=message):
        build_explorer().read_files([path.name], start_line=start_line, end_line=end_line)


def test_refuses_a_line_range_that_starts_past_the_end_of_a_file(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("one\ntwo\n")

    with pytest.raises(RuntimeError, match="it ends at line 2, before line 5"):
        build_explorer().read_files([path.name], start_line=5)


def test_reads_a_file_whose_contents_read_like_a_file_header(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    body = "Ready.\n\nFile:\n```\n/etc/passwd\n```\n"
    path.write_text(body)

    result = build_explorer().read_files([path.name])

    assert result == [
        {"type": "input_text", "text": f"File:\n```\n{path}\n```"},
        {"type": "input_text", "text": f"Content:\n````\n{body}\n````"},
    ]


def test_reads_an_image_as_an_image_input(tmp_path: Path) -> None:
    path = tmp_path / "diagram.png"
    path.write_bytes(PNG_HEADER)

    result = build_explorer().read_files([path.name])

    assert result[1] == {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{base64.b64encode(PNG_HEADER).decode()}",
    }


# The README promises local files "including images", and the size of
# a real one is the whole promise: the tests above hold for an
# eight-byte header whatever a tool call does with a screenshot.
def test_reads_a_screenshot_at_the_size_a_screen_makes_one(tmp_path: Path) -> None:
    path = tmp_path / "screenshot.png"
    data = PNG_HEADER + b"\x00" * (150 * KILOBYTE)
    path.write_bytes(data)

    invocation = Invocation(build_explorer().read_files([path.name]))
    list(invocation)

    assert invocation.output[1] == {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{base64.b64encode(data).decode()}",
    }


def test_reads_undecodable_bytes_as_a_file_input(tmp_path: Path) -> None:
    path = tmp_path / "archive.bin"
    path.write_bytes(UNDECODABLE)

    result = build_explorer().read_files([path.name])

    assert result[1] == {
        "type": "input_file",
        "filename": "archive.bin",
        "file_data": base64.b64encode(UNDECODABLE).decode(),
    }


def test_rejects_a_file_over_the_input_size_limit(tmp_path: Path) -> None:
    path = tmp_path / "huge.txt"
    with path.open("wb") as file:
        file.truncate(Explorer.MAX_INPUT_SIZE + 1)

    with pytest.raises(RuntimeError, match=f"exceeds {Explorer.MAX_INPUT_SIZE} bytes"):
        build_explorer().read_files([path.name])


def test_reports_unreadable_paths_without_logging_a_crash(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="jri"), pytest.raises(RuntimeError, match=r"missing\.txt"):
        build_explorer().read_files(["missing.txt"])

    record = next(record for record in caplog.records if record.message.startswith("read_failed"))
    assert record.levelno == logging.WARNING
    assert record.exc_info is None


def test_runs_shell_commands_in_the_directory_it_was_given(tmp_path: Path) -> None:
    directory = tmp_path / "elsewhere"
    directory.mkdir()
    (directory / "marker.txt").write_text("here\n")

    assert "here\n" in build_explorer(directory).run_shell("cat marker.txt")


def test_reads_relative_paths_from_the_directory_it_was_given(tmp_path: Path) -> None:
    directory = tmp_path / "elsewhere"
    directory.mkdir()
    (directory / "notes.md").write_text("Notes\n")

    result = build_explorer(directory).read_files(["notes.md"])

    assert result[0] == {"type": "input_text", "text": f"File:\n```\n{directory / 'notes.md'}\n```"}


def test_reports_a_failing_shell_command() -> None:
    with pytest.raises(RuntimeError, match="Command exited with status 3") as failure:
        build_explorer().run_shell("echo nope; exit 3")

    assert "nope" in str(failure.value)


def test_reads_at_most_the_maximum_shell_output(tmp_path: Path) -> None:
    (tmp_path / "wide.txt").write_text("x" * (Invocation.MAX_OUTPUT_LENGTH + 100))

    output = build_explorer().run_shell("cat wide.txt")

    assert output == "x" * Invocation.MAX_OUTPUT_LENGTH


def test_stops_the_process_group_when_a_shell_command_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    background = tmp_path / "background.pid"
    serve_timeout(monkeypatch, background)

    with pytest.raises(RuntimeError, match="Command timed out after 30 seconds"):
        build_explorer().run_shell("sleep 60 & echo $! > background.pid; sleep 60")

    child = int(background.read_text())
    deadline = time.monotonic() + 10
    while _is_running(child) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _is_running(child)


def test_reports_a_timeout_whose_process_group_already_vanished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def vanish(_group: int, _number: int) -> None:
        raise ProcessLookupError

    serve_timeout(monkeypatch, tmp_path / "done.txt")
    monkeypatch.setattr(os, "killpg", vanish)

    with pytest.raises(RuntimeError, match="Command timed out after 30 seconds"):
        build_explorer().run_shell("echo finished > done.txt")


def test_searches_the_web_and_quotes_the_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_API_KEY", "search-key")
    provider = FakeProvider(respond(200, {"grounding": {"generic": RESULTS}}))
    monkeypatch.setattr(brave.httpx, "post", provider.post)
    explorer = Explorer(build_settings(FakeClient([]), search_api_key="SEARCH_API_KEY"), Path.cwd())

    output = explorer.search_web("how to ralph")

    assert output == (
        "Search results:\n  https://justralph.it: Just Ralph It\n"
        "  https://ghuntley.com/ralph: Ralph Wiggum as a software engineer"
    )
    assert provider.calls[0][1]["X-Subscription-Token"] == "search-key"


def test_withholds_web_search_without_an_api_key() -> None:
    with_key = Explorer(build_settings(FakeClient([]), search_api_key="SEARCH_API_KEY"), Path.cwd())

    assert "search_web" not in [tool.name for tool in build_explorer().tools]
    assert "search_web" in [tool.name for tool in with_key.tools]


def test_fetches_a_page_as_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_pages(monkeypatch, lambda _request: httpx.Response(200, html="<h1>Delivery</h1><p>Deploy from <b>main</b>."))

    output = build_explorer().fetch_web_page("https://example.test/docs")

    assert "Delivery" in output
    assert "**main**" in output


def test_fetches_a_youtube_url_as_its_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "YouTubeTranscriptApi", lambda: FakeApi([], []))

    assert build_explorer().fetch_web_page("https://youtu.be/abc123") == TRANSCRIPT


def test_stops_fetching_a_page_at_the_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    chunk_size = Explorer.MAX_INPUT_SIZE * 2 // 3
    chunks = [letter.encode() * chunk_size for letter in "abc"]
    served: list[bytes] = []
    serve_chunks(monkeypatch, chunks, served)

    output = build_explorer().fetch_web_page("https://example.test/page")

    assert output == "a" * chunk_size + "b" * (Explorer.MAX_INPUT_SIZE - chunk_size)
    assert served == chunks[:2]


def test_reports_a_page_the_host_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_pages(monkeypatch, lambda _request: httpx.Response(404, text="Not found"))

    # The row a fetch opens names the URL, so a reason that names it
    # again is the same address read twice.
    with pytest.raises(RuntimeError, match=r"^404 Not Found$"):
        build_explorer().fetch_web_page("https://example.test/missing")


def test_reports_a_url_that_cannot_be_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_pages(monkeypatch, lambda _request: httpx.Response(200, text="Reached"))

    with pytest.raises(RuntimeError, match="Invalid non-printable ASCII character in URL"):
        build_explorer().fetch_web_page("https://example.test/\x00")


def test_reports_a_page_whose_failure_carries_no_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def time_out(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("")

    serve_pages(monkeypatch, time_out)

    with pytest.raises(RuntimeError, match=r"^ConnectTimeout$"):
        build_explorer().fetch_web_page("https://example.test/docs")


def test_reports_a_page_that_never_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    serve_pages(monkeypatch, refuse)

    with pytest.raises(RuntimeError, match="connection refused"):
        build_explorer().fetch_web_page("https://example.test/docs")


def test_names_a_read_row_after_the_files_it_covers() -> None:
    read_files = find_read_files(build_explorer())
    arguments = json.dumps({
        "paths": [str(Path.cwd() / "README.md"), str(Path.cwd() / "pyproject.toml"), "src/app.py", "uv.lock"],
        "start_line": None,
        "end_line": None,
    })

    assert read_files.format_label(read_files.finished_label, arguments) == (
        "Read README.md, pyproject.toml, src/app.py and 1 more"
    )


# The row reads the arguments too, so a call whose row says "and 1
# more" still reads every file the model asked for.
def test_reads_the_paths_a_call_names_rather_than_the_row_describing_them(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("one\n")
    read_files = find_read_files(build_explorer(tmp_path))

    invocation = read_files.invoke(json.dumps({"paths": [str(path)], "start_line": None, "end_line": None}))
    list(invocation)

    assert invocation.outcome == "done"
    assert invocation.output == [
        {"type": "input_text", "text": f"File:\n```\n{path}\n```"},
        {"type": "input_text", "text": "Content:\n```\none\n\n```"},
    ]


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
