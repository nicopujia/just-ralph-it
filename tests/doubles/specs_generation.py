from collections.abc import Generator, Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jri.core.settings import Settings

COMMIT = "1a2b3c4"


def generate_interrupted(_settings: "Settings", _active_commit: str | None) -> Iterator[object]:
    yield object()


def generate_succeeding(_settings: "Settings", _active_commit: str | None) -> Generator[object, None, str]:
    yield object()
    return COMMIT
