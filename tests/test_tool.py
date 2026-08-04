from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from openai import omit

from jri.core.ai import Explorer, Invocation
from jri.lib import brave, youtube
from tests.doubles.brave import RESULTS, FakeProvider, respond
from tests.doubles.openai import FakeClient, reply, response
from tests.doubles.settings import build_settings
from tests.doubles.web import serve_chunks, serve_pages
from tests.doubles.youtube import TRANSCRIPT, FakeApi

if TYPE_CHECKING:
    from openai.types.responses import ResponseFunctionCallOutputItemListParam


TRUNCATION_NOTICE = "[Output truncated. Try splitting into more targeted calls.]"


def build_explorer(path: Path) -> Explorer:
    return Explorer(build_settings(path, FakeClient([])))


@pytest.mark.parametrize("temperature", [0, None], ids=["configured", "omitted"])
def test_sends_temperature_only_when_configured(tmp_path: Path, temperature: float | None) -> None:
    client = FakeClient([response(reply("Explored."))])

    list(Explorer(build_settings(tmp_path, client, temperature=temperature)).send_message("Study this."))

    assert client.responses.options[-1]["temperature"] == (omit if temperature is None else temperature)


def test_truncates_long_text_output() -> None:
    invocation = Invocation("x" * (Invocation.MAX_OUTPUT_LENGTH + 1))
    list(invocation)

    output = cast("str", invocation.output)

    assert output == "x" * Invocation.MAX_OUTPUT_LENGTH + f"\n\n{TRUNCATION_NOTICE}"


def test_truncates_long_structured_output() -> None:
    output = cast(
        "ResponseFunctionCallOutputItemListParam",
        [
            {"type": "input_text", "text": "first"},
            {"type": "input_text", "text": "x" * Invocation.MAX_OUTPUT_LENGTH},
            {"type": "input_text", "text": "omitted"},
        ],
    )

    invocation = Invocation(output)
    list(invocation)
    result = cast("ResponseFunctionCallOutputItemListParam", invocation.output)

    truncated = cast("dict[str, str]", result[1])["text"]
    assert result[0] == output[0]
    assert truncated.startswith("x" * (Invocation.MAX_OUTPUT_LENGTH - len("first")))
    assert truncated.endswith(TRUNCATION_NOTICE)
    assert len(result) == len(output) - 1


def test_reads_a_selected_range_of_lines(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("one\ntwo\nthree\nfour\n")

    result = build_explorer(tmp_path).read_files([path.name], start_line=2, end_line=3)

    assert result[1] == {"type": "input_text", "text": "two\nthree\n"}


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
