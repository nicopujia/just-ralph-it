import logging
from time import monotonic
from typing import cast, overload

import httpx

__all__ = ["forget_catalog", "get_input_room", "get_limit", "read_catalog"]

ENDPOINT = "https://models.dev/models.json"
# A read that gets no answer waits this long. The catalog is one file. An endpoint that needs longer does not
# answer.
TIMEOUT = 30.0
# JRI reads the catalog again this long after a read that failed.
# An agent measures its request on each of its rounds. A read on each round adds `TIMEOUT` to each round.
# A read that failed also gives each agent the fallback room, which is smaller than the room of a model.
# This delay limits both costs. It stops the reads of a session, and it lets a session that starts offline
# read the true limits later.
RETRY_DELAY = 300.0

logger = logging.getLogger(__name__)

# JRI holds the catalog of the one read that answered. It holds no catalog before that read.
_catalog: dict[str, object] | None = None
# JRI holds the time of the last read that failed. It holds no time before a read fails.
_failed_at: float | None = None


# The room is the part of a model window that one request can use. A catalog entry states the room, or states
# the window and the largest answer, which leaves the rest as room. An answer that fills the whole window leaves
# no room. JRI uses the fallback for such an entry, and for an entry it cannot read.
def get_input_room(model: str, fallback: int) -> int:
    limits = _read_limits(model)
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
    published = _read_limits(model).get("context")
    return fallback if published is None else published


# Read the catalog one time, and hold it for all the process. A read that fails gives no catalog, and each
# caller then uses its own fallback. Hold a read that failed for `RETRY_DELAY`, and read nothing inside it.
def read_catalog() -> dict[str, object]:
    global _catalog, _failed_at  # noqa: PLW0603
    if _catalog is not None:
        return _catalog
    if _failed_at is not None and monotonic() - _failed_at < RETRY_DELAY:
        return {}
    try:
        response = httpx.get(ENDPOINT, timeout=TIMEOUT)
        response.raise_for_status()
        catalog = response.json()
    except (httpx.HTTPError, ValueError):
        logger.exception("catalog_read_failed")
        _failed_at = monotonic()
        return {}
    if not isinstance(catalog, dict):
        logger.error("catalog_unreadable")
        _failed_at = monotonic()
        return {}
    _catalog = cast("dict[str, object]", catalog)
    return _catalog


# Drop the catalog that JRI holds, and the time of the last read that failed. The next read starts again.
def forget_catalog() -> None:
    global _catalog, _failed_at  # noqa: PLW0603
    _catalog = None
    _failed_at = None


# Read all the limits of a model together. A caller reads several limits for one answer, and the catalog gives
# them in one entry. A catalog that names no such model gives no limit at all.
def _read_limits(model: str) -> dict[str, int]:
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
