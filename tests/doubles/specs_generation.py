from collections.abc import Generator, Iterator
from threading import Event
from typing import TYPE_CHECKING

from jri.core.ai import ToolCallFinished, ToolCallStarted
from jri.core.exceptions import RepositoryStateError

if TYPE_CHECKING:
    from jri.core.settings import Settings

COMMIT = "1a2b3c4"
FINISHED_ROW = ToolCallFinished("commit", "Saved the specifications to your project", "done")
STARTED_ROW = ToolCallStarted("commit", "Saving the specifications to your project", "💾")


def generate_blocked(_settings: "Settings", _cancelled: Event | None = None) -> Iterator[object]:
    yield STARTED_ROW
    raise RepositoryStateError("Your project has uncommitted changes.")


def generate_failing(_settings: "Settings", _cancelled: Event | None = None) -> Iterator[object]:
    yield STARTED_ROW
    raise RuntimeError("The architect could not be reached.")


def generate_interrupted(_settings: "Settings", _cancelled: Event | None = None) -> Iterator[object]:
    yield object()


def generate_succeeding(_settings: "Settings", _cancelled: Event | None = None) -> Generator[object, None, str]:
    yield STARTED_ROW
    yield FINISHED_ROW
    return COMMIT


# The workflow answers a stop by returning no result at all, so a run
# that never hears about one commits instead.
def generate_stopped(_settings: "Settings", cancelled: Event | None = None) -> Generator[object, None, str | None]:
    yield STARTED_ROW
    if cancelled is not None and cancelled.is_set():
        return None
    yield FINISHED_ROW
    return COMMIT
