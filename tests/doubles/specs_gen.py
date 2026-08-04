from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jri.core.settings import Settings


class InterruptibleSpecsGen:
    def __init__(self, _settings: "Settings") -> None:
        pass

    @staticmethod
    def generate(_active_commit: str | None) -> Iterator[object]:
        yield object()
