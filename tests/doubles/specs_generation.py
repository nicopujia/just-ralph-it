from collections.abc import Generator, Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jri.core.settings import Settings


class InterruptibleSpecsGeneration:
    def __init__(self, _settings: "Settings") -> None:
        pass

    @staticmethod
    def generate(_active_commit: str | None) -> Iterator[object]:
        yield object()


class SucceedingSpecsGeneration:
    COMMIT = "1a2b3c4"

    def __init__(self, _settings: "Settings") -> None:
        pass

    @staticmethod
    def generate(_active_commit: str | None) -> Generator[object, None, str]:
        yield object()
        return SucceedingSpecsGeneration.COMMIT
