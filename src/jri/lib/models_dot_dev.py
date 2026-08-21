import logging
from functools import cache
from typing import cast, overload

import httpx

__all__ = ["get_input_room", "get_limit", "read_limits"]

ENDPOINT = "https://models.dev/models.json"

logger = logging.getLogger(__name__)


# The room is the part of a model window that one request can use. A catalog entry states the room, or states
# the window and the largest answer, which leaves the rest as room. An answer that fills the whole window leaves
# no room. JRI uses the fallback for such an entry, and for an entry it cannot read.
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


# Cache all the limits of a model together, and not one limit at a time. A caller reads several limits for one
# answer. A cache of one limit fetches the catalog again for each other limit. The caller holds the fallback,
# so this function does not cache it. `cache` does not store an exception. JRI reads the catalog again on the
# next call after a failure.
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
