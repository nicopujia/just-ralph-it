"""Model metadata and context-size helpers."""

import json
from functools import cache
from typing import cast

import httpx

__all__ = ["estimate_tokens", "get_context_limit"]

ENDPOINT = "https://models.dev/models.json"
FALLBACK_CONTEXT_LIMIT = 100_000


@cache
def get_context_limit(model: str) -> int:
    """Return the model's context-window limit from models.dev.

    Returns:
        The model's maximum context size in tokens.

    Uses a conservative fallback when models.dev is unavailable or does
    not contain the configured model.
    """

    try:
        catalog = _fetch_catalog()
    except (RuntimeError, TypeError):
        return FALLBACK_CONTEXT_LIMIT
    entry = catalog.get(model)
    if entry is None:
        suffix = model.rsplit("/", 1)[-1]
        matches = [value for key, value in catalog.items() if key.rsplit("/", 1)[-1] == suffix]
        if len(matches) != 1:
            return FALLBACK_CONTEXT_LIMIT
        entry = matches[0]
    limit = cast("dict[str, object]", entry.get("limit", {})).get("context")
    if not isinstance(limit, int):
        return FALLBACK_CONTEXT_LIMIT
    return limit


def estimate_tokens(context: object, tools: object) -> int:
    """Estimate tokens from the serialized request size.

    Returns:
        A conservative token-count estimate.
    """

    payload = json.dumps({"input": context, "tools": tools}, ensure_ascii=False, separators=(",", ":"))
    return (len(payload.encode()) + 2) // 3


def _fetch_catalog() -> dict[str, dict[str, object]]:
    try:
        response = httpx.get(ENDPOINT, timeout=30.0)
        response.raise_for_status()
        catalog = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise RuntimeError(f"Failed to load models.dev: {error}") from error
    if not isinstance(catalog, dict):
        raise TypeError("models.dev returned an invalid catalog.")
    return cast("dict[str, dict[str, object]]", catalog)
