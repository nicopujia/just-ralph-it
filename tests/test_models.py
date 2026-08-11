import httpx
import pytest

from jri.core.settings import AgentProfiles
from jri.lib.models import estimate_tokens, get_context_limit
from tests.doubles.models import build_response, serve_catalog, serve_outcome

CONTEXT_LIMIT = 273_000
CATALOG = {"openai/gpt-5.6-sol": {"limit": {"context": CONTEXT_LIMIT}}}
FALLBACK = 9_000


# Check the behavior in `test_reads_the_context_limit_the_catalog_really_publishes`.
# Check the behavior in `test_reads_the_context_limit_the_catalog_really_publishes`.
# Check the behavior in `test_reads_the_context_limit_the_catalog_really_publishes`.
@pytest.mark.contract
def test_reads_the_context_limit_the_catalog_really_publishes() -> None:
    assert get_context_limit(AgentProfiles().interviewer.model) is not None


def test_reads_the_context_limit_of_a_catalogued_model(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, CATALOG)

    assert get_context_limit("openai/gpt-5.6-sol") == CONTEXT_LIMIT


def test_matches_a_model_offered_under_another_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, CATALOG)

    assert get_context_limit("gpt-5.6-sol") == CONTEXT_LIMIT


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

    assert get_context_limit("gpt-5.6-sol", FALLBACK) == FALLBACK


def test_answers_with_nothing_when_no_fallback_is_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, {})

    assert get_context_limit("gpt-5.6-sol") is None


@pytest.mark.parametrize(
    "catalog",
    [{"openai/gpt-5.6-sol": CONTEXT_LIMIT}, {"openai/gpt-5.6-sol": {"limit": CONTEXT_LIMIT}}],
    ids=["entry-not-an-object", "limit-not-an-object"],
)
def test_falls_back_when_a_catalog_entry_is_malformed(monkeypatch: pytest.MonkeyPatch, catalog: object) -> None:
    serve_catalog(monkeypatch, catalog)

    assert get_context_limit("openai/gpt-5.6-sol", FALLBACK) == FALLBACK


def test_prefers_an_exact_match_over_an_ambiguous_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, {**CATALOG, "azure/gpt-5.6-sol": {"limit": {"context": 128_000}}})

    assert get_context_limit("openai/gpt-5.6-sol") == CONTEXT_LIMIT


def test_falls_back_when_the_catalog_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, CATALOG, status_code=503)

    assert get_context_limit("openai/gpt-5.6-sol", FALLBACK) == FALLBACK


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

    assert get_context_limit("openai/gpt-5.6-sol", FALLBACK) == FALLBACK


def test_reads_the_catalog_again_after_a_read_that_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_outcome(monkeypatch, httpx.ConnectError("connection refused"), build_response(CATALOG))

    assert get_context_limit("openai/gpt-5.6-sol", FALLBACK) == FALLBACK
    assert get_context_limit("openai/gpt-5.6-sol", FALLBACK) == CONTEXT_LIMIT


def test_reads_the_catalog_once_for_a_model_it_answered_for(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_outcome(monkeypatch, build_response(CATALOG), httpx.ConnectError("connection refused"))

    assert get_context_limit("openai/gpt-5.6-sol", FALLBACK) == CONTEXT_LIMIT
    assert get_context_limit("openai/gpt-5.6-sol", FALLBACK) == CONTEXT_LIMIT


def test_estimates_tokens_from_the_byte_size_of_the_payload() -> None:
    # Check the behavior in `test_estimates_tokens_from_the_byte_size_of_the_payload`.
    # Check the behavior in `test_estimates_tokens_from_the_byte_size_of_the_payload`.
    assert estimate_tokens("é" * 300, None) == estimate_tokens("a" * 300, None) + 100


def test_estimates_the_tokens_of_the_tools_alongside_the_context() -> None:
    assert estimate_tokens("prompt", [{"name": "search"}]) > estimate_tokens("prompt", None)
