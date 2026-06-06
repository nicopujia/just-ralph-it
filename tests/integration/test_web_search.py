"""Live smoke tests for web search providers."""

import asyncio

import pytest

from jri.core.tools.explore import search_web


def test_live_brave_web_search_smoke(
    runtime_env: dict[str, str],
    live: bool,
) -> None:
    """Brave Search returns compact web results in live mode."""
    if not live:
        pytest.skip("use --live to run real Brave Search smoke tests")
    api_key = runtime_env.get("BRAVE_SEARCH_API_KEY")
    if not api_key:
        pytest.fail("BRAVE_SEARCH_API_KEY is required for --live")

    result = asyncio.run(search_web(query="Brave Search API", api_key=api_key))

    assert result.startswith("Search results:")
    assert "brave" in result.lower()
    assert api_key not in result
