from typing import cast

import httpx

RESULTS = [
    {"title": "Just Ralph It", "url": "https://justralph.it"},
    {"title": "Ralph Wiggum as a software engineer", "url": "https://ghuntley.com/ralph"},
]


class FakeProvider:
    """Search endpoint recording the request it is called with."""

    def __init__(self, outcome: httpx.Response | httpx.HTTPError) -> None:
        self.outcome = outcome
        self.calls: list[tuple[dict[str, str], dict[str, str]]] = []

    def post(self, _url: str, **options: object) -> httpx.Response:
        self.calls.append((cast("dict[str, str]", options["json"]), cast("dict[str, str]", options["headers"])))
        if isinstance(self.outcome, httpx.HTTPError):
            raise self.outcome
        return self.outcome


def respond(status_code: int, body: object = None) -> httpx.Response:
    """Build a search response the provider would return.

    Returns:
        A response bound to a stand-in request.
    """

    request = httpx.Request("POST", "https://search.test/context")
    if body is None:
        return httpx.Response(status_code, request=request)
    return httpx.Response(status_code, json=body, request=request)
