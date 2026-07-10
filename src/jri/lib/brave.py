"""Search the web with the Brave LLM Context API."""

from dataclasses import dataclass
from typing import cast

import httpx

__all__ = ["Error", "SearchResult", "search"]

_ENDPOINT = "https://api.search.brave.com/res/v1/llm/context"


@dataclass
class SearchResult:
    """A web search result."""

    url: str
    title: str


class Error(Exception):
    """Raised when the Brave API returns an error."""


def search(api_key: str, query: str) -> list[SearchResult]:
    """Search the web and return generic results.

    Returns:
        Generic web search results.

    Raises:
        Error: If the Brave request fails.
    """

    try:
        response = httpx.post(
            _ENDPOINT,
            json={"query": query},
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            timeout=30.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        if isinstance(error, httpx.HTTPStatusError):
            try:
                detail = str(cast("dict[str, object]", error.response.json())["error"])
            except (KeyError, TypeError, ValueError):
                detail = error.response.text or str(error)
        else:
            detail = str(error)
        raise Error(detail) from error

    raw = cast("dict[str, object]", response.json())
    grounding = cast("dict[str, object]", raw["grounding"])
    return [
        SearchResult(url=cast("str", item["url"]), title=cast("str", item["title"]))
        for item in cast("list[dict[str, object]]", grounding["generic"])
    ]
