import logging
from typing import cast

import httpx

__all__ = ["search"]

ENDPOINT = "https://api.search.brave.com/res/v1/llm/context"

logger = logging.getLogger(__name__)


def search(api_key: str, query: str) -> list[dict[str, str]]:
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

    try:
        body = cast("object", response.json())
    except ValueError as error:
        logger.exception("search_failed query=%r", query)
        raise RuntimeError(f"Brave answered with something other than JSON: {response.text!r}") from error
    match body:
        case {"grounding": {"generic": list() as generic}}:
            results = cast("list[dict[str, str]]", generic)
        case _:
            logger.error("search_failed query=%r response_body=%r", query, response.text)
            raise RuntimeError(f"Brave answered without any results: {response.text!r}")
    logger.info("search_finished results=%d", len(results))
    logger.debug(
        "search_response url=%r status_code=%r headers=%r response_body=%r",
        str(response.url),
        response.status_code,
        dict(response.headers),
        response.text,
    )
    return results
