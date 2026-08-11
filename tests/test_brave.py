import httpx
import pytest

from jri.lib import brave
from tests.conftest import ReadCredential
from tests.doubles.brave import RESULTS, FakeProvider, respond

API_KEY_VARIABLE = "BRAVE_SEARCH_API_KEY"


# Check the behavior in `test_searches_the_endpoint_brave_really_serves`.
# Check the behavior in `test_searches_the_endpoint_brave_really_serves`.
@pytest.mark.contract
def test_searches_the_endpoint_brave_really_serves(read_credential: ReadCredential) -> None:
    results = brave.search(read_credential(API_KEY_VARIABLE), "just ralph it")

    assert results
    assert all(result["title"] and result["url"] for result in results)


def test_returns_the_generic_results_of_a_successful_search(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider(respond(200, {"grounding": {"generic": RESULTS, "other": []}}))
    monkeypatch.setattr(brave.httpx, "post", provider.post)

    assert brave.search("search-key", "how to ralph") == RESULTS
    assert provider.calls == [
        ({"q": "how to ralph"}, {"Accept": "application/json", "X-Subscription-Token": "search-key"})
    ]


def test_returns_no_results_when_the_provider_finds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brave.httpx, "post", FakeProvider(respond(200, {"grounding": {"generic": []}})).post)

    assert brave.search("search-key", "how to ralph") == []


@pytest.mark.parametrize(
    "body",
    [{"other": {}}, {"grounding": {"other": []}}, {"grounding": ["Just Ralph It"]}, {"grounding": None}],
    ids=["no-grounding", "no-generic", "grounding-not-a-mapping", "grounding-is-null"],
)
def test_reports_an_accepted_response_that_carries_no_results(monkeypatch: pytest.MonkeyPatch, body: object) -> None:
    monkeypatch.setattr(brave.httpx, "post", FakeProvider(respond(200, body)).post)

    with pytest.raises(RuntimeError):
        brave.search("search-key", "how to ralph")


@pytest.mark.parametrize(
    "response", [respond(200, text="<html>Gateway timeout</html>"), respond(204)], ids=["not-json", "no-body"]
)
def test_reports_an_accepted_response_that_is_not_json(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    monkeypatch.setattr(brave.httpx, "post", FakeProvider(response).post)

    with pytest.raises(RuntimeError):
        brave.search("search-key", "how to ralph")


def test_reports_the_detail_the_provider_explains_a_rejection_with(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brave.httpx, "post", FakeProvider(respond(422, {"error": "Query is too long."})).post)

    with pytest.raises(RuntimeError, match=r"Query is too long\."):
        brave.search("search-key", "how to ralph")


@pytest.mark.parametrize(
    "body",
    [{"message": "Rate limited."}, ["Rate limited."], "Rate limited."],
    ids=["no-error-field", "not-an-object", "not-a-mapping"],
)
def test_reports_the_response_body_when_no_detail_is_given(monkeypatch: pytest.MonkeyPatch, body: object) -> None:
    monkeypatch.setattr(brave.httpx, "post", FakeProvider(respond(429, body)).post)

    with pytest.raises(RuntimeError, match=r"Rate limited\."):
        brave.search("search-key", "how to ralph")


def test_reports_a_rejection_with_an_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brave.httpx, "post", FakeProvider(respond(429)).post)

    with pytest.raises(RuntimeError, match="429 Too Many Requests"):
        brave.search("search-key", "how to ralph")


def test_reports_a_search_that_never_reached_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brave.httpx, "post", FakeProvider(httpx.ConnectError("connection refused")).post)

    with pytest.raises(RuntimeError, match="connection refused"):
        brave.search("search-key", "how to ralph")
