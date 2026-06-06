# pyright: reportUnannotatedClassAttribute=false
"""Tests for read-only exploration."""

import asyncio
from pathlib import Path

import pytest

from jri.core.tools.explore import (
    BraveSearchError,
    BraveSearchOptions,
    explore_context,
    fetch_url,
    glob_paths,
    grep_text,
    read_text,
    search_web,
)
from tests.doubles.explorers import RecordingExplorer
from tests.doubles.http import (
    FakeBraveClient,
    FakeClient,
    PayloadBraveClient,
)


def test_explore_invokes_explorer_with_plain_language_request(
    tmp_path: Path,
) -> None:
    """Explore delegates the request to the explorer subagent."""
    explorer = RecordingExplorer("Summary:\n- Found the CLI entrypoint.")

    result = asyncio.run(
        explore_context(
            project_root=tmp_path,
            request="Find the CLI entrypoint.",
            explorer=explorer,
        )
    )

    assert result == "Summary:\n- Found the CLI entrypoint."
    assert explorer.requests == [(tmp_path, "Find the CLI entrypoint.")]


def test_explorer_file_tools_are_read_only(tmp_path: Path) -> None:
    """Explorer helpers read existing project files without mutation."""
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('hi')\n")

    assert glob_paths(pattern="**/*.py", root=tmp_path) == ["src/app.py"]
    assert read_text(path=source, root=tmp_path) == "1: print('hi')"
    assert source.read_text() == "print('hi')\n"


def test_explorer_file_tools_do_not_expose_jri_logs(
    tmp_path: Path,
) -> None:
    """Raw logs are telemetry, not interviewer working memory."""
    logs = tmp_path / ".jri" / "logs"
    logs.mkdir(parents=True)
    (logs / "interview.jsonl").write_text('{"type": "secret"}\n')

    assert glob_paths(pattern="**/*", root=tmp_path) == []
    assert grep_text(pattern="secret", root=tmp_path) == ""
    with pytest.raises(ValueError, match="logs"):
        read_text(path=logs / "interview.jsonl", root=tmp_path)


def test_explorer_file_tools_do_not_expose_secret_files(
    tmp_path: Path,
) -> None:
    """Environment and key files are not readable by explorer helpers."""
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=secret\n")
    (tmp_path / ".env.local").write_text("BRAVE_SEARCH_API_KEY=secret\n")
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    private_key = ssh_dir / "id_ed25519"
    private_key.write_text("private key\n")
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('safe')\n")

    assert glob_paths(pattern="**/*", root=tmp_path) == ["src/app.py"]
    assert grep_text(pattern="secret", root=tmp_path) == ""
    with pytest.raises(ValueError, match="secret files"):
        read_text(path=tmp_path / ".env", root=tmp_path)
    with pytest.raises(ValueError, match="secret files"):
        read_text(path=private_key, root=tmp_path)


def test_read_text_rejects_paths_outside_project_root(
    tmp_path: Path,
) -> None:
    """Explorer reads stay inside the project root."""
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside\n")

    with pytest.raises(ValueError, match="project root"):
        read_text(path=outside, root=tmp_path)


def test_grep_text_limits_matches_and_skips_binary_files(
    tmp_path: Path,
) -> None:
    """Grep returns bounded text matches from readable files."""
    source = tmp_path / "src" / "app.py"
    binary = tmp_path / "src" / "image.bin"
    source.parent.mkdir()
    source.write_text("alpha\nalpha again\nbeta\n")
    binary.write_bytes(b"\xff\xfe")

    assert grep_text(pattern="alpha", root=tmp_path, limit=1) == (
        "src/app.py:1: alpha"
    )
    assert grep_text(pattern="missing", root=tmp_path) == ""


def test_web_search_uses_brave_search_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web search returns compact Brave Search results."""
    fake_client = FakeBraveClient()
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        fake_client.create,
    )

    result = asyncio.run(
        search_web(
            query="python cli",
            api_key="brave-key",
            options=BraveSearchOptions(
                count=2,
                country="US",
                search_lang="en",
            ),
        )
    )

    assert fake_client.headers["X-Subscription-Token"] == "brave-key"
    assert fake_client.params == {
        "q": "python cli",
        "count": 2,
        "country": "US",
        "search_lang": "en",
        "result_filter": "web",
    }
    assert result == (
        "Search results:\n"
        "1. Python CLI docs\n"
        "   URL: https://example.com/python-cli\n"
        "   Snippet: Build command line tools in Python.\n"
        "2. argparse tutorial\n"
        "   URL: https://example.com/argparse\n"
        "   Snippet: argparse helps parse CLI flags."
    )


def test_web_search_requires_brave_search_api_key() -> None:
    """Web search fails clearly without Brave credentials."""
    with pytest.raises(BraveSearchError, match="BRAVE_SEARCH_API_KEY"):
        asyncio.run(search_web(query="python cli", api_key=None))


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"web": None},
        {"web": {"results": {}}},
        {"web": {"results": [None]}},
    ],
)
def test_web_search_returns_no_results_for_empty_brave_payloads(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    """Web search handles empty Brave payload variants."""
    fake_client = PayloadBraveClient(payload)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        fake_client.create,
    )

    result = asyncio.run(search_web(query="python cli", api_key="brave-key"))

    assert result == "No web results found."


def test_web_search_uses_fallbacks_for_missing_result_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web search formats malformed individual results safely."""
    empty_result: dict[str, object] = {}
    payload: dict[str, object] = {"web": {"results": [empty_result]}}
    fake_client = PayloadBraveClient(payload)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        fake_client.create,
    )

    result = asyncio.run(search_web(query="python cli", api_key="brave-key"))

    assert result == (
        "Search results:\n1. Untitled result\n   URL: unknown\n   Snippet: "
    )


def test_fetch_url_returns_bounded_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URL fetching reports status, final URL, type, and bounded body."""
    monkeypatch.setattr("jri.core.tools.explore.httpx.AsyncClient", FakeClient)

    result = asyncio.run(fetch_url("https://example.com"))

    assert "Status: 200" in result
    assert "Final URL: https://example.com/final" in result
    assert "Content-Type: text/plain" in result
    assert len(result) < 20_200
