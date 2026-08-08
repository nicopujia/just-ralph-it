from collections.abc import Generator, Iterator
from threading import Event
from typing import TYPE_CHECKING

from jri.core.ai import ReasoningDelta, ToolCallFinished, ToolCallStarted
from jri.core.exceptions import RepositoryStateError

if TYPE_CHECKING:
    from jri.core.settings import Settings

COMMIT = "1a2b3c4"
FINISHED_ROW = ToolCallFinished("commit", "Saved the specifications to your project", "done")
STARTED_ROW = ToolCallStarted("commit", "Saving the specifications to your project", "💾")
THOUGHT = ReasoningDelta("Weighing the options.")
# A stop that never arrives would hang the suite, and one that is going
# to arrive is a poll away.
STOPS_WITHIN = 10.0


def generate_blocked(_settings: "Settings", _cancelled: Event | None = None) -> Iterator[object]:
    yield STARTED_ROW
    raise RepositoryStateError("Your project has uncommitted changes.")


def generate_failing(_settings: "Settings", _cancelled: Event | None = None) -> Iterator[object]:
    yield STARTED_ROW
    raise RuntimeError("The architect could not be reached.")


def generate_succeeding(_settings: "Settings", _cancelled: Event | None = None) -> Generator[object, None, str]:
    yield STARTED_ROW
    yield FINISHED_ROW
    return COMMIT


def generate_thinking(_settings: "Settings", _cancelled: Event | None = None) -> Generator[object, None, str]:
    yield STARTED_ROW
    yield THOUGHT
    yield FINISHED_ROW
    return COMMIT


# Most of a run is a model call saying nothing for minutes, and a stop
# has to reach it there rather than at the next thing it writes down.
def generate_silently(_settings: "Settings", cancelled: Event | None = None) -> Generator[object, None, str | None]:
    assert cancelled is not None
    assert cancelled.wait(STOPS_WITHIN), "the stop never reached the run"
    return None
    yield


# The workflow answers a stop by returning no result at all. A run in a
# process of its own hears about one only once the file the follower
# writes reaches its watcher, so this waits for the stop rather than
# looking for one that could not have arrived yet.
def generate_stopped(_settings: "Settings", cancelled: Event | None = None) -> Generator[object, None, str | None]:
    yield STARTED_ROW
    assert cancelled is not None
    assert cancelled.wait(STOPS_WITHIN), "the stop never reached the run"
    return None
