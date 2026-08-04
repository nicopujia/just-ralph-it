import base64
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from jri.core.ai import Explorer, Invocation
from jri.lib import brave, youtube
from tests.doubles.brave import RESULTS, FakeProvider, respond
from tests.doubles.openai import FakeClient
from tests.doubles.settings import build_settings
from tests.doubles.web import serve_chunks, serve_pages
from tests.doubles.youtube import TRANSCRIPT, FakeApi

PNG_HEADER = b"\x89PNG\r\n\x1a\n"
UNDECODABLE = b"\xff\xfe\x00binary"


def build_explorer(path: Path) -> Explorer:
    return Explorer(build_settings(path, FakeClient([])))


def test_reads_a_selected_range_of_lines(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("one\ntwo\nthree\nfour\n")

    result = build_explorer(tmp_path).read_files([path.name], start_line=2, end_line=3)

    assert result[0] == {"type": "input_text", "text": f"File: {path}"}
    assert result[1] == {"type": "input_text", "text": "two\nthree\n"}


def test_reads_an_image_as_an_image_input(tmp_path: Path) -> None:
    path = tmp_path / "diagram.png"
    path.write_bytes(PNG_HEADER)

    result = build_explorer(tmp_path).read_files([path.name])

    assert result[1] == {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{base64.b64encode(PNG_HEADER).decode()}",
    }


def test_reads_undecodable_bytes_as_a_file_input(tmp_path: Path) -> None:
    path = tmp_path / "archive.bin"
    path.write_bytes(UNDECODABLE)

    result = build_explorer(tmp_path).read_files([path.name])

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
        build_explorer(tmp_path).read_files([path.name])


def test_reports_unreadable_paths(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"missing\.txt"):
        build_explorer(tmp_path).read_files(["missing.txt"])


def test_runs_shell_commands_in_the_working_directory(tmp_path: Path) -> None:
    (tmp_path / "marker.txt").write_text("here\n")

    assert "here\n" in build_explorer(tmp_path).shell("cat marker.txt")


def test_reports_a_failing_shell_command(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Command exited with status 3") as failure:
        build_explorer(tmp_path).shell("echo nope; exit 3")

    assert "nope" in str(failure.value)


def test_reads_at_most_the_maximum_shell_output(tmp_path: Path) -> None:
    length = Invocation.MAX_OUTPUT_LENGTH + 100

    output = build_explorer(tmp_path).shell(f"head -c {length} /dev/zero | tr '\\0' 'x'")

    assert output == "x" * Invocation.MAX_OUTPUT_LENGTH


def test_stops_the_process_group_when_a_shell_command_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    background = tmp_path / "background.pid"
    original_wait = subprocess.Popen.wait

    def wait(process: "subprocess.Popen[bytes]", timeout: float | None = None) -> int:
        if timeout is None:
            return original_wait(process)
        deadline = time.monotonic() + 10
        while not background.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise subprocess.TimeoutExpired(process.args, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", wait)

    with pytest.raises(RuntimeError, match="Command timed out after 30 seconds"):
        build_explorer(tmp_path).shell("sleep 60 & echo $! > background.pid; sleep 60")

    child = int(background.read_text())
    deadline = time.monotonic() + 10
    while _is_running(child) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _is_running(child)


def test_searches_the_web_and_links_the_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_API_KEY", "search-key")
    provider = FakeProvider(respond(200, {"grounding": {"generic": RESULTS}}))
    monkeypatch.setattr(brave.httpx, "post", provider.post)
    explorer = Explorer(build_settings(tmp_path, FakeClient([]), search_api_key="SEARCH_API_KEY"))

    output = explorer.web_search("how to ralph")

    assert output == (
        "- [Just Ralph It](https://justralph.it)\n- [Ralph Wiggum as a software engineer](https://ghuntley.com/ralph)"
    )
    assert provider.calls[0][1]["X-Subscription-Token"] == "search-key"


def test_reports_web_search_as_unavailable_without_an_api_key(tmp_path: Path) -> None:
    assert build_explorer(tmp_path).web_search("how to ralph") == "Web search not available."


def test_fetches_a_page_as_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    serve_pages(monkeypatch, lambda _request: httpx.Response(200, html="<h1>Delivery</h1><p>Deploy from <b>main</b>."))

    output = build_explorer(tmp_path).web_fetch("https://example.test/docs")

    assert "Delivery" in output
    assert "**main**" in output


def test_fetches_a_youtube_url_as_its_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "YouTubeTranscriptApi", lambda: FakeApi([], []))

    assert build_explorer(tmp_path).web_fetch("https://youtu.be/abc123") == TRANSCRIPT


def test_stops_fetching_a_page_at_the_size_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chunk_size = Explorer.MAX_INPUT_SIZE * 2 // 3
    chunks = [letter.encode() * chunk_size for letter in "abc"]
    served: list[bytes] = []
    serve_chunks(monkeypatch, chunks, served)

    output = build_explorer(tmp_path).web_fetch("https://example.test/page")

    assert output == "a" * chunk_size + "b" * (Explorer.MAX_INPUT_SIZE - chunk_size)
    assert served == chunks[:2]


def test_reports_a_page_the_host_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    serve_pages(monkeypatch, lambda _request: httpx.Response(404, text="Not found"))

    with pytest.raises(RuntimeError, match=r"Could not fetch https://example\.test/missing"):
        build_explorer(tmp_path).web_fetch("https://example.test/missing")


def test_reports_a_page_that_never_answered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    serve_pages(monkeypatch, refuse)

    with pytest.raises(RuntimeError, match="connection refused"):
        build_explorer(tmp_path).web_fetch("https://example.test/docs")


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
