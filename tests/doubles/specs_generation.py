from collections.abc import Generator, Iterator
from typing import TYPE_CHECKING

from jri.core.ai import ToolCallFinished, ToolCallStarted
from jri.core.exceptions import RepositoryStateError

if TYPE_CHECKING:
    from jri.core.settings import Settings

COMMIT = "1a2b3c4"
FINISHED_ROW = ToolCallFinished("commit", "Saved the specifications to your project", "done")
STARTED_ROW = ToolCallStarted("commit", "Saving the specifications to your project", "💾")


def generate_blocked(_settings: "Settings") -> Iterator[object]:
    yield STARTED_ROW
    raise RepositoryStateError("Your project has uncommitted changes.")


def generate_interrupted(_settings: "Settings") -> Iterator[object]:
    yield object()


def generate_succeeding(_settings: "Settings") -> Generator[object, None, str]:
    yield STARTED_ROW
    yield FINISHED_ROW
    return COMMIT
