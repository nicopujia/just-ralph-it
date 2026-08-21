import logging

import httpx
import pytest

from jri.core.settings import AgentProfiles
from jri.lib.models_dot_dev import get_input_room, get_limit
from tests.doubles.models_dot_dev import build_response, serve_catalog, serve_outcome

CONTEXT_LIMIT = 273_000
INPUT_ROOM = 272_000
OUTPUT_LIMIT = 128_000
WINDOW = 400_000
CATALOG = {"openai/gpt-5.6-sol": {"limit": {"context": CONTEXT_LIMIT}}}
FALLBACK = 9_000


# This is the one live check against models.dev. Every other test here trusts a double that could drift from
# what the real catalog actually serves for the configured model.
@pytest.mark.contract
def test_reads_the_context_limit_the_catalog_really_publishes() -> None:
    assert get_limit(AgentProfiles().interviewer.model) is not None


# A catalog states the room a request has, or states the window and the largest answer, which leaves the rest of
# the window to the request.
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


# A catalogued model can state a window no larger than the answer it can write into that window. Such an entry
# leaves a request no room at all, and it says as little about the model as an entry JRI cannot read.
def test_falls_back_to_a_room_when_the_window_leaves_a_request_none(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, {"openai/gpt-5.6-sol": {"limit": {"context": OUTPUT_LIMIT, "output": OUTPUT_LIMIT}}})

    assert get_input_room("openai/gpt-5.6-sol", FALLBACK) == FALLBACK


# A window one token wider than the answer leaves the request that one token, and JRI works against it. Only a
# window that leaves nothing at all falls back.
def test_reads_the_room_a_window_leaves_beside_the_largest_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = {"openai/gpt-5.6-sol": {"limit": {"context": OUTPUT_LIMIT + 1, "output": OUTPUT_LIMIT}}}
    serve_catalog(monkeypatch, catalog)

    assert get_input_room("openai/gpt-5.6-sol", FALLBACK) == 1


def test_falls_back_to_a_room_when_the_catalog_states_no_window(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, {"openai/gpt-5.6-sol": {}})

    assert get_input_room("openai/gpt-5.6-sol", FALLBACK) == FALLBACK


# One room comes from several published limits. The catalog answers for the whole model at once, so the answer
# stands even when the endpoint stops answering after the first read.
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


# A catalog JRI cannot read leaves every model on its fallback, and the run says nothing about it. The log
# carries the reason, so a reader can tell a fallback from a published limit.
def test_logs_a_catalog_read_that_failed(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    serve_outcome(monkeypatch, httpx.ConnectError("connection refused"))

    with caplog.at_level(logging.ERROR, logger="jri"):
        get_input_room("openai/gpt-5.6-sol", FALLBACK)

    assert [record.getMessage() for record in caplog.records] == ["catalog_read_failed model='openai/gpt-5.6-sol'"]


def test_reads_the_catalog_again_after_a_read_that_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_outcome(monkeypatch, httpx.ConnectError("connection refused"), build_response(CATALOG))

    assert get_limit("openai/gpt-5.6-sol", FALLBACK) == FALLBACK
    assert get_limit("openai/gpt-5.6-sol", FALLBACK) == CONTEXT_LIMIT


def test_reads_the_catalog_once_for_a_model_it_answered_for(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_outcome(monkeypatch, build_response(CATALOG), httpx.ConnectError("connection refused"))

    assert get_limit("openai/gpt-5.6-sol", FALLBACK) == CONTEXT_LIMIT
    assert get_limit("openai/gpt-5.6-sol", FALLBACK) == CONTEXT_LIMIT
