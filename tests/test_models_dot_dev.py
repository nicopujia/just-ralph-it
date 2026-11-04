import logging

import httpx
import pytest

from jri.core.settings import AgentProfiles
from jri.lib import models_dot_dev
from jri.lib.models_dot_dev import RETRY_DELAY, forget_catalog, get_input_room, get_limit
from tests.doubles.models_dot_dev import build_response, serve_catalog, serve_outcome

CONTEXT_LIMIT = 273_000
INPUT_ROOM = 272_000
OUTPUT_LIMIT = 128_000
WINDOW = 400_000
CATALOG = {"openai/gpt-5.6-sol": {"limit": {"context": CONTEXT_LIMIT}}}
FALLBACK = 9_000


# This is the only test that reads models.dev. Every other test here trusts a double. A double can disagree
# with the catalog that models.dev serves for the configured model.
@pytest.mark.contract
def test_reads_the_context_limit_the_catalog_really_publishes() -> None:
    assert get_limit(AgentProfiles().interviewer.model) is not None


# A catalog states the room that a request has. Or it states the window and the largest answer, and the request
# gets the rest of the window.
def test_reads_the_input_room_a_catalogue_publishes(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, {"openai/gpt-5.6-sol": {"limit": {"context": WINDOW, "input": INPUT_ROOM}}})

    assert get_input_room("openai/gpt-5.6-sol", FALLBACK) == INPUT_ROOM


def test_leaves_the_room_the_window_holds_beside_the_largest_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, {"openai/gpt-5.6-sol": {"limit": {"context": WINDOW, "output": OUTPUT_LIMIT}}})

    assert get_input_room("openai/gpt-5.6-sol", FALLBACK) == INPUT_ROOM


# A catalog that names no largest answer leaves the whole window to the request.
def test_leaves_the_whole_window_to_a_request_when_the_catalog_names_no_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, {"openai/gpt-5.6-sol": {"limit": {"context": WINDOW}}})

    assert get_input_room("openai/gpt-5.6-sol", FALLBACK) == WINDOW


# A catalog entry can state a window no larger than the largest answer. Such an entry leaves the request no
# room. It tells JRI as little as an entry that JRI cannot read, so JRI uses the fallback room.
def test_falls_back_to_a_room_when_the_window_leaves_a_request_none(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, {"openai/gpt-5.6-sol": {"limit": {"context": OUTPUT_LIMIT, "output": OUTPUT_LIMIT}}})

    assert get_input_room("openai/gpt-5.6-sol", FALLBACK) == FALLBACK


# A window one token wider than the largest answer leaves the request one token, and JRI uses that one token.
# Only a window that leaves no token makes JRI use the fallback room.
def test_reads_the_room_a_window_leaves_beside_the_largest_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = {"openai/gpt-5.6-sol": {"limit": {"context": OUTPUT_LIMIT + 1, "output": OUTPUT_LIMIT}}}
    serve_catalog(monkeypatch, catalog)

    assert get_input_room("openai/gpt-5.6-sol", FALLBACK) == 1


def test_falls_back_to_a_room_when_the_catalog_states_no_window(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, {"openai/gpt-5.6-sol": {}})

    assert get_input_room("openai/gpt-5.6-sol", FALLBACK) == FALLBACK


# JRI calculates one room from more than one published limit. The catalog gives all the limits of a model in one
# answer. JRI still knows the room when the endpoint stops answering after the first read.
def test_reads_the_catalog_once_for_the_room_it_answers_with(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = {"openai/gpt-5.6-sol": {"limit": {"context": WINDOW, "output": OUTPUT_LIMIT}}}
    serve_outcome(monkeypatch, build_response(catalog), httpx.ConnectError("connection refused"))

    assert get_input_room("openai/gpt-5.6-sol", FALLBACK) == INPUT_ROOM


def test_reads_the_context_limit_of_a_catalogued_model(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, CATALOG)

    assert get_limit("openai/gpt-5.6-sol") == CONTEXT_LIMIT


def test_matches_a_model_offered_under_another_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, CATALOG)

    assert get_limit("gpt-5.6-sol") == CONTEXT_LIMIT


@pytest.mark.parametrize(
    "catalog",
    [
        {"openai/other": {"limit": {"context": CONTEXT_LIMIT}}},
        {"openai/gpt-5.6-sol": {}},
        {"openai/gpt-5.6-sol": {"limit": {"context": "large"}}},
        {**CATALOG, "azure/gpt-5.6-sol": {"limit": {"context": 128_000}}},
        [],
    ],
    ids=["unknown-model", "no-limit", "unusable-limit", "ambiguous-suffix", "not-a-catalog"],
)
def test_falls_back_when_the_catalog_cannot_answer(monkeypatch: pytest.MonkeyPatch, catalog: object) -> None:
    serve_catalog(monkeypatch, catalog)

    assert get_limit("gpt-5.6-sol", FALLBACK) == FALLBACK


def test_answers_with_nothing_when_no_fallback_is_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, {})

    assert get_limit("gpt-5.6-sol") is None


@pytest.mark.parametrize(
    "catalog",
    [{"openai/gpt-5.6-sol": CONTEXT_LIMIT}, {"openai/gpt-5.6-sol": {"limit": CONTEXT_LIMIT}}],
    ids=["entry-not-an-object", "limit-not-an-object"],
)
def test_falls_back_when_a_catalog_entry_is_malformed(monkeypatch: pytest.MonkeyPatch, catalog: object) -> None:
    serve_catalog(monkeypatch, catalog)

    assert get_limit("openai/gpt-5.6-sol", FALLBACK) == FALLBACK


def test_prefers_an_exact_match_over_an_ambiguous_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, {**CATALOG, "azure/gpt-5.6-sol": {"limit": {"context": 128_000}}})

    assert get_limit("openai/gpt-5.6-sol") == CONTEXT_LIMIT


def test_falls_back_when_the_catalog_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, CATALOG, status_code=503)

    assert get_limit("openai/gpt-5.6-sol", FALLBACK) == FALLBACK


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.ConnectError("connection refused"),
        httpx.Response(200, text="<html>Gateway timeout</html>", request=httpx.Request("GET", "https://models.test")),
    ],
    ids=["transport-failure", "not-json"],
)
def test_falls_back_when_the_catalog_response_is_unusable(
    monkeypatch: pytest.MonkeyPatch, outcome: httpx.Response | httpx.HTTPError
) -> None:
    serve_outcome(monkeypatch, outcome)

    assert get_limit("openai/gpt-5.6-sol", FALLBACK) == FALLBACK


# JRI uses the fallback room for every model when it cannot read the catalog, and the run shows nothing about
# this. The log gives the reason, and a reader knows if a room is a fallback or a published limit.
def test_logs_a_catalog_read_that_failed(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    serve_outcome(monkeypatch, httpx.ConnectError("connection refused"))

    with caplog.at_level(logging.ERROR, logger="jri"):
        get_input_room("openai/gpt-5.6-sol", FALLBACK)

    assert [record.getMessage() for record in caplog.records] == ["catalog_read_failed"]


# An agent measures its request on each round, and each round reads a limit. An endpoint that does not answer
# holds each of those reads for the timeout. Hold a read that failed, so that only the first round waits.
def test_holds_a_read_that_failed_and_reads_nothing_inside_the_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_outcome(monkeypatch, httpx.ConnectError("connection refused"), build_response(CATALOG))

    assert get_limit("openai/gpt-5.6-sol", FALLBACK) == FALLBACK
    assert get_limit("openai/gpt-5.6-sol", FALLBACK) == FALLBACK


# A read that failed gives every agent the fallback room, which is smaller than the room of a model. JRI must
# not hold that for all a session. Read the catalog again after the delay.
def test_reads_the_catalog_again_after_the_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_outcome(monkeypatch, httpx.ConnectError("connection refused"), build_response(CATALOG))
    clock = [0.0]
    monkeypatch.setattr(models_dot_dev, "monotonic", lambda: clock[0])

    assert get_limit("openai/gpt-5.6-sol", FALLBACK) == FALLBACK
    clock[0] = RETRY_DELAY

    assert get_limit("openai/gpt-5.6-sol", FALLBACK) == CONTEXT_LIMIT


# A read that gets no answer must not wait without end. An agent waits with it, and the user waits for
# the agent.
def test_waits_a_limited_time_for_the_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    waited: list[object] = []

    def get(_url: str, **options: object) -> httpx.Response:
        waited.append(options["timeout"])
        return build_response(CATALOG)

    monkeypatch.setattr(httpx, "get", get)
    forget_catalog()

    get_limit("openai/gpt-5.6-sol")

    assert waited == [30.0]


# A catalog that JRI cannot read is not a catalog that failed to arrive. Name the two in different words, so
# that a reader of the log knows which one occurred.
def test_logs_a_catalog_it_could_not_read(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    serve_catalog(monkeypatch, [])

    with caplog.at_level(logging.ERROR, logger="jri"):
        get_input_room("openai/gpt-5.6-sol", FALLBACK)

    assert [record.getMessage() for record in caplog.records] == ["catalog_unreadable"]


def test_reads_the_catalog_once_for_a_model_it_answered_for(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_outcome(monkeypatch, build_response(CATALOG), httpx.ConnectError("connection refused"))

    assert get_limit("openai/gpt-5.6-sol", FALLBACK) == CONTEXT_LIMIT
    assert get_limit("openai/gpt-5.6-sol", FALLBACK) == CONTEXT_LIMIT
