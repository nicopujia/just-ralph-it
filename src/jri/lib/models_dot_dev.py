import logging
from functools import cache
from typing import Literal, cast, overload

import httpx

__all__ = ["get_input_room", "get_limit", "read_limit"]

# The three limits a catalog entry can publish: the whole window, the part of it a request can fill, and the
# largest answer a model can write.
type Limit = Literal["context", "input", "output"]

ENDPOINT = "https://models.dev/models.json"

logger = logging.getLogger(__name__)


# This is the room a request has. A catalog entry states it, or states the window and the largest answer that a
# model can write into it, which leaves the rest of the window to the request.
def get_input_room(model: str, fallback: int) -> int:
    published = get_limit(model, limit="input")
    if published is not None:
        return published
    context = get_limit(model)
    return fallback if context is None else context - get_limit(model, 0, "output")


@overload
def get_limit(model: str, fallback: int, limit: Limit = "context") -> int: ...


@overload
def get_limit(model: str, fallback: None = None, limit: Limit = "context") -> int | None: ...


def get_limit(model: str, fallback: int | None = None, limit: Limit = "context") -> int | None:
    try:
        published = read_limit(model, limit)
    except (RuntimeError, TypeError):
        logger.exception("catalog_read_failed model=%r", model)
        published = None
    return fallback if published is None else published


# Cache only the catalog value. The caller owns the fallback. `cache` does not store exceptions. JRI retries a
# failed catalog read on the next call.
@cache
def read_limit(model: str, limit: Limit = "context") -> int | None:
    catalog = _fetch_catalog()
    entry = catalog.get(model)
    if entry is None:
        suffix = model.rsplit("/", 1)[-1]
        matches = [value for key, value in catalog.items() if key.rsplit("/", 1)[-1] == suffix]
        if len(matches) != 1:
            return None
        entry = matches[0]
    match entry:
        case {"limit": {**limits}}:
            published = limits.get(limit)
            return published if isinstance(published, int) else None
        case _:
            return None


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
