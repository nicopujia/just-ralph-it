from collections.abc import Generator, Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jri.core.settings import Settings

COMMIT = "1a2b3c4"


def generate_interrupted(_settings: "Settings") -> Iterator[object]:
    yield object()


def generate_succeeding(_settings: "Settings") -> Generator[object, None, str]:
    yield object()
    return COMMIT
