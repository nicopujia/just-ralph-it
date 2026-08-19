from collections.abc import Generator, Iterator
from threading import Event
from typing import TYPE_CHECKING

from jri.core.ai import ReasoningDelta, ToolCallFinished, ToolCallStarted, specs_generation
from jri.core.exceptions import (
    NotebookTooLargeError,
    ProviderRefusalError,
    ProviderUnavailableError,
    RepositoryStateError,
)

if TYPE_CHECKING:
    from jri.core.settings import Settings

COMMIT = "1a2b3c4"
FINISHED_ROW = ToolCallFinished("commit", "Saved the specifications to your project", "done")
STARTED_ROW = ToolCallStarted("commit", "Saving the specifications to your project", "💾")
THOUGHT = ReasoningDelta("Weighing the options.")
STREAMED_THOUGHT = "part {number} "
STREAMED_THOUGHTS = 200
# A stop that never arrives would hang the suite. A stop that will arrive is one poll away.
STOPS_WITHIN = 10.0
# A run that changes nothing closes its own rows. It saves no specifications, so it reports none saved.
UNCHANGED_FINISHED_ROW = ToolCallFinished("commit", "Your project already holds these specifications", "done")
UNCHANGED_STARTED_ROW = ToolCallStarted("commit", "Comparing the specifications with your project", "💾")


def generate_blocked(_settings: "Settings", _cancelled: Event | None = None) -> Iterator[object]:
    yield STARTED_ROW
    raise RepositoryStateError("Your project has uncommitted changes.")


# The notebook does not fit one model call. Every run of this notebook ends here until JRI carries a notebook of
# this size, so no retry of the user changes the result.
def generate_oversized(_settings: "Settings", _cancelled: Event | None = None) -> Iterator[object]:
    yield STARTED_ROW
    raise NotebookTooLargeError("The notebook is too large to write specifications from.")


def generate_failing(_settings: "Settings", _cancelled: Event | None = None) -> Iterator[object]:
    yield STARTED_ROW
    raise RuntimeError("The architect could not be reached.")


# The architect's model does not offer the reasoning effort that the request asks for. The provider answers the same
# way on every attempt of every run.
def generate_refused(_settings: "Settings", _cancelled: Event | None = None) -> Iterator[object]:
    yield STARTED_ROW
    raise ProviderRefusalError("The provider answered 400 Bad Request, saying:\n```\nUnsupported value.\n```")


# Nothing answers at the provider address. A run cannot reach the model, and every retry of that run fails the same
# way. A later run can still succeed, so this is not a refusal.
def generate_unavailable(_settings: "Settings", _cancelled: Event | None = None) -> Iterator[object]:
    yield STARTED_ROW
    raise ProviderUnavailableError("Could not reach the provider at https://provider.test/v1/: connection refused")


def generate_succeeding(_settings: "Settings", _cancelled: Event | None = None) -> Generator[object, None, str]:
    yield STARTED_ROW
    yield FINISHED_ROW
    return COMMIT


def generate_unchanged(
    _settings: "Settings", _cancelled: Event | None = None
) -> Generator[object, None, specs_generation.Unchanged]:
    yield UNCHANGED_STARTED_ROW
    yield UNCHANGED_FINISHED_ROW
    return specs_generation.Unchanged()


def generate_thinking(_settings: "Settings", _cancelled: Event | None = None) -> Generator[object, None, str]:
    yield STARTED_ROW
    yield THOUGHT
    yield FINISHED_ROW
    return COMMIT


def generate_streaming(_settings: "Settings", _cancelled: Event | None = None) -> Generator[object, None, str]:
    yield STARTED_ROW
    for number in range(STREAMED_THOUGHTS):
        yield ReasoningDelta(STREAMED_THOUGHT.format(number=number))
    yield FINISHED_ROW
    return COMMIT


# A run spends most of its minutes in a model call that says nothing. A stop must reach the run there, and not at
# the next thing that the run writes down.
def generate_silently(_settings: "Settings", cancelled: Event | None = None) -> Generator[object, None, str | None]:
    assert cancelled is not None
    assert cancelled.wait(STOPS_WITHIN), "the stop never reached the run"
    return None
    yield


# The workflow answers a stop with no result at all. A run in a process of its own hears about a stop only after the
# file that the follower writes reaches its watcher. This double waits for the stop. A test of the flag at this
# point would find no stop, because no stop could have arrived yet.
def generate_stopped(_settings: "Settings", cancelled: Event | None = None) -> Generator[object, None, str | None]:
    yield STARTED_ROW
    assert cancelled is not None
    assert cancelled.wait(STOPS_WITHIN), "the stop never reached the run"
    return None
