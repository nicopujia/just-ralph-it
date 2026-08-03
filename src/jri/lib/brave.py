"""Search the web with the Brave LLM Context API."""

import logging
from typing import cast

import httpx

__all__ = ["search"]

ENDPOINT = "https://api.search.brave.com/res/v1/llm/context"

logger = logging.getLogger(__name__)


def search(api_key: str, query: str) -> list[dict[str, str]]:
    """Search the web and return generic results.

    Returns:
        Generic web search results.

    Raises:
        RuntimeError: If the Brave request fails.
    """

    logger.debug("search_query query=%r", query)
    try:
        response = httpx.post(
            ENDPOINT,
            json={"query": query},
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            timeout=30.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        if isinstance(error, httpx.HTTPStatusError):
            logger.debug(
                "search_error_response url=%r headers=%r response_body=%r",
                str(error.response.url),
                dict(error.response.headers),
                error.response.text,
            )
            logger.exception(
                "search_failed query=%r url=%r status_code=%r",
                query,
                str(error.response.url),
                error.response.status_code,
            )
            try:
                detail = str(cast("dict[str, object]", error.response.json())["error"])
            except (KeyError, TypeError, ValueError):
                detail = error.response.text or str(error)
        else:
            logger.exception("search_failed query=%r", query)
            detail = str(error)
        raise RuntimeError(detail) from error

    raw = cast("dict[str, object]", response.json())
    grounding = cast("dict[str, object]", raw["grounding"])
    results = cast("list[dict[str, str]]", grounding["generic"])
    logger.info("search_finished results=%d", len(results))
    logger.debug(
        "search_response url=%r status_code=%r headers=%r response_body=%r",
        str(response.url),
        response.status_code,
        dict(response.headers),
        response.text,
    )
    return results
