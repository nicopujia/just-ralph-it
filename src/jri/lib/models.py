import json
import logging
from functools import cache
from typing import cast, overload

import httpx

__all__ = ["estimate_tokens", "get_context_limit", "read_context_limit"]

ENDPOINT = "https://models.dev/models.json"

logger = logging.getLogger(__name__)


@overload
def get_context_limit(model: str, fallback: int) -> int: ...


@overload
def get_context_limit(model: str, fallback: None = None) -> int | None: ...


def get_context_limit(model: str, fallback: int | None = None) -> int | None:
    try:
        limit = read_context_limit(model)
    except (RuntimeError, TypeError):
        logger.exception("catalog_read_failed model=%r", model)
        limit = None
    return fallback if limit is None else limit


# Cache only the catalog value. The caller owns the fallback. `cache` does not store exceptions. JRI retries a
# failed catalog read on the next call.
@cache
def read_context_limit(model: str) -> int | None:
    catalog = _fetch_catalog()
    entry = catalog.get(model)
    if entry is None:
        suffix = model.rsplit("/", 1)[-1]
        matches = [value for key, value in catalog.items() if key.rsplit("/", 1)[-1] == suffix]
        if len(matches) != 1:
            return None
        entry = matches[0]
    match entry:
        case {"limit": {"context": int() as limit}}:
            return limit
        case _:
            return None


def estimate_tokens(context: object, tools: object) -> int:
    payload = json.dumps({"input": context, "tools": tools}, ensure_ascii=False, separators=(",", ":"))
    return (len(payload.encode()) + 2) // 3


def _fetch_catalog() -> dict[str, object]:
    try:
        response = httpx.get(ENDPOINT, timeout=30.0)
        response.raise_for_status()
        catalog = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise RuntimeError(f"Failed to load models.dev: {error}") from error
    if not isinstance(catalog, dict):
        raise TypeError("models.dev returned an invalid catalog.")
    return cast("dict[str, object]", catalog)
