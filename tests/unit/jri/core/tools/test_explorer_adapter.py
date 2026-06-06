# pyright: reportArgumentType=false, reportInvalidCast=false
"""Tests for explorer tool adapters."""

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from jri.core.tools.explore import BraveSearchOptions
from jri.core.tools.explorer import (
    ExplorerDeps,
    build_explorer_tools,
    curl_tool,
    glob_tool,
    grep_tool,
    read_tool,
    web_search_tool,
)
from tests.doubles.agents import FakeRunContext

if TYPE_CHECKING:
    from pydantic_ai import RunContext


def test_build_explorer_tools_preserves_model_visible_names() -> None:
    """Explorer tools register with stable model-visible names."""
    tools = build_explorer_tools()

    assert [tool.name for tool in tools] == [
        "glob",
        "grep",
        "read",
        "curl",
        "web_search",
    ]
    assert [tool.takes_ctx for tool in tools] == [
        True,
        True,
        True,
        False,
        False,
    ]


def test_explorer_tool_wrappers(tmp_path: Path) -> None:
    """Explorer tool wrappers call read-only helpers."""
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('hello')\n")
    ctx = cast(
        "RunContext[ExplorerDeps]", FakeRunContext(ExplorerDeps(tmp_path))
    )

    assert glob_tool(ctx, "**/*.py") == "src/app.py"
    assert "src/app.py:1" in grep_tool(ctx, "hello", include="**/*.py")
    assert read_tool(ctx, "src/app.py") == "1: print('hello')"


def test_explorer_read_tool_rejects_external_absolute_paths(
    tmp_path: Path,
) -> None:
    """Explorer reads are scoped to the active project root."""
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside\n")
    ctx = cast(
        "RunContext[ExplorerDeps]", FakeRunContext(ExplorerDeps(tmp_path))
    )

    with pytest.raises(ValueError, match="project root"):
        read_tool(ctx, str(outside))


def test_web_search_tool_uses_brave_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explorer web search delegates to Brave Search."""
    captured: dict[str, object] = {}

    async def fake_search_web(**kwargs: object) -> str:
        captured.update(kwargs)
        return "Search results:\n1. Result"

    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    monkeypatch.setattr("jri.core.tools.explorer.search_web", fake_search_web)

    result = asyncio.run(web_search_tool("python cli", count=3))

    assert result == "Search results:\n1. Result"
    assert captured == {
        "query": "python cli",
        "api_key": "brave-key",
        "options": BraveSearchOptions(
            count=3,
            country="US",
            search_lang="en",
        ),
    }


def test_curl_tool_uses_fetch_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Curl tool delegates to the bounded URL fetcher."""

    async def fetch(url: str) -> str:
        return f"fetched {url}"

    monkeypatch.setattr("jri.core.tools.explorer.fetch_url", fetch)

    assert asyncio.run(curl_tool("https://example.com")) == (
        "fetched https://example.com"
    )
