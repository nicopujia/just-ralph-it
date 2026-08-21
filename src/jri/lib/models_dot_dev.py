import logging
from functools import cache
from typing import cast, overload

import httpx

__all__ = ["get_input_room", "get_limit", "read_limits"]

ENDPOINT = "https://models.dev/models.json"

logger = logging.getLogger(__name__)


# This is the room a request has. A catalog entry states it, or states the window and the largest answer that a
# model can write into it, which leaves the rest of the window to the request. An entry whose answer fills the
# whole window leaves the request nothing, and it says as little about the model as an entry JRI cannot read.
def get_input_room(model: str, fallback: int) -> int:
    limits = _get_limits(model)
    room = limits.get("input")
    if room is None:
        window = limits.get("context")
        room = None if window is None else window - limits.get("output", 0)
    return fallback if room is None or room <= 0 else room


@overload
def get_limit(model: str, fallback: int) -> int: ...


@overload
def get_limit(model: str, fallback: None = None) -> int | None: ...


def get_limit(model: str, fallback: int | None = None) -> int | None:
    published = _get_limits(model).get("context")
    return fallback if published is None else published


# Cache every limit of a model together, and not one limit at a time: a caller reads several of them for one
# answer, and a catalog it reads one at a time is a catalog it fetches once for each. The caller owns the
# fallback. `cache` does not store exceptions. JRI retries a failed catalog read on the next call.
@cache
def read_limits(model: str) -> dict[str, int]:
    catalog = _fetch_catalog()
    entry = catalog.get(model)
    if entry is None:
        suffix = model.rsplit("/", 1)[-1]
        matches = [value for key, value in catalog.items() if key.rsplit("/", 1)[-1] == suffix]
        if len(matches) != 1:
            return {}
        entry = matches[0]
    match entry:
        case {"limit": {**limits}}:
            published = cast("dict[str, object]", limits)
            return {name: value for name, value in published.items() if isinstance(value, int)}
        case _:
            return {}


def _get_limits(model: str) -> dict[str, int]:
    try:
        return read_limits(model)
    except (RuntimeError, TypeError):
        logger.exception("catalog_read_failed model=%r", model)
        return {}


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
