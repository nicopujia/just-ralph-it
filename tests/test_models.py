import httpx
import pytest

from jri.lib.models import FALLBACK_CONTEXT_LIMIT, estimate_tokens, get_context_limit
from tests.doubles.models import serve_catalog, serve_outcome

CONTEXT_LIMIT = 273_000
CATALOG = {"openai/gpt-5.6-sol": {"limit": {"context": CONTEXT_LIMIT}}}


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

    assert get_context_limit("gpt-5.6-sol") == FALLBACK_CONTEXT_LIMIT


@pytest.mark.xfail(
    strict=True,
    reason="get_context_limit crashes on a catalog entry or limit that is not a mapping instead of falling back",
)
@pytest.mark.parametrize(
    "catalog",
    [{"openai/gpt-5.6-sol": CONTEXT_LIMIT}, {"openai/gpt-5.6-sol": {"limit": CONTEXT_LIMIT}}],
    ids=["entry-not-an-object", "limit-not-an-object"],
)
def test_falls_back_when_a_catalog_entry_is_malformed(monkeypatch: pytest.MonkeyPatch, catalog: object) -> None:
    serve_catalog(monkeypatch, catalog)

    assert get_context_limit("openai/gpt-5.6-sol") == FALLBACK_CONTEXT_LIMIT


def test_prefers_an_exact_match_over_an_ambiguous_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, {**CATALOG, "azure/gpt-5.6-sol": {"limit": {"context": 128_000}}})

    assert get_context_limit("openai/gpt-5.6-sol") == CONTEXT_LIMIT


def test_falls_back_when_the_catalog_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch, CATALOG, status_code=503)

    assert get_context_limit("openai/gpt-5.6-sol") == FALLBACK_CONTEXT_LIMIT


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

    assert get_context_limit("openai/gpt-5.6-sol") == FALLBACK_CONTEXT_LIMIT


def test_estimates_tokens_from_the_byte_size_of_the_payload() -> None:
    # Three utf-8 bytes make a token, so 300 two-byte characters
    # cost 100 tokens more than 300 one-byte ones.
    assert estimate_tokens("é" * 300, None) == estimate_tokens("a" * 300, None) + 100


def test_estimates_the_tokens_of_the_tools_alongside_the_context() -> None:
    assert estimate_tokens("prompt", [{"name": "search"}]) > estimate_tokens("prompt", None)
