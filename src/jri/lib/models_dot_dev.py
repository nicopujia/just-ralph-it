import logging
from functools import cache
from typing import Literal, cast, overload

import httpx

__all__ = ["get_input_room", "get_limit", "read_limit"]

# The fields a catalog entry publishes under `limit`: the whole window, the part of it a request can fill, and
# the largest answer a model can write.
type Field = Literal["context", "input", "output"]

ENDPOINT = "https://models.dev/models.json"

logger = logging.getLogger(__name__)


# This is the room a request has. A catalog entry states it, or states the window and the largest answer that a
# model can write into it, which leaves the rest of the window to the request. An entry whose answer fills the
# whole window leaves the request nothing, and it says as little about the model as an entry JRI cannot read.
def get_input_room(model: str, fallback: int) -> int:
    room = get_limit(model, field="input")
    if room is None:
        context = get_limit(model)
        room = None if context is None else context - get_limit(model, 0, "output")
    return fallback if room is None or room <= 0 else room


@overload
def get_limit(model: str, fallback: int, field: Field = "context") -> int: ...


@overload
def get_limit(model: str, fallback: None = None, field: Field = "context") -> int | None: ...


def get_limit(model: str, fallback: int | None = None, field: Field = "context") -> int | None:
    try:
        limit = read_limit(model, field)
    except (RuntimeError, TypeError):
        logger.exception("catalog_read_failed model=%r", model)
        limit = None
    return fallback if limit is None else limit


# Cache only the catalog value. The caller owns the fallback. `cache` does not store exceptions. JRI retries a
# failed catalog read on the next call.
@cache
def read_limit(model: str, field: Field) -> int | None:
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
            limit = limits.get(field)
            return limit if isinstance(limit, int) else None
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
