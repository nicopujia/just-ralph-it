import logging
from functools import cache
from typing import cast, overload

import httpx

__all__ = ["get_input_room", "get_limit", "read_catalog", "read_limits"]

ENDPOINT = "https://models.dev/models.json"
# A read that gets no answer waits this long. The catalog is one file, and an endpoint that needs longer than
# this does not answer.
TIMEOUT = 30.0

logger = logging.getLogger(__name__)


# The room is the part of a model window that one request can use. A catalog entry states the room, or states
# the window and the largest answer, which leaves the rest as room. An answer that fills the whole window leaves
# no room. JRI uses the fallback for such an entry, and for an entry it cannot read.
def get_input_room(model: str, fallback: int) -> int:
    limits = read_limits(model)
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
    published = read_limits(model).get("context")
    return fallback if published is None else published


# Read all the limits of a model together, and not one limit at a time. A caller reads several limits for one
# answer. The caller holds the fallback, so this function does not use it.
def read_limits(model: str) -> dict[str, int]:
    catalog = read_catalog()
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


# Read the catalog one time in a process, and hold what that read gives, including nothing. A read that fails
# gives no limits, and each caller uses its own fallback. Hold a failed read too: an agent measures its request
# on each of its rounds, and a read of an endpoint that does not answer waits for the timeout above. A read on
# each round adds that wait to each round.
@cache
def read_catalog() -> dict[str, object]:
    try:
        response = httpx.get(ENDPOINT, timeout=TIMEOUT)
        response.raise_for_status()
        catalog = response.json()
    except (httpx.HTTPError, ValueError):
        logger.exception("catalog_read_failed")
        return {}
    if not isinstance(catalog, dict):
        logger.error("catalog_unreadable")
        return {}
    return cast("dict[str, object]", catalog)
