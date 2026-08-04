from typing import cast

import httpx

RESULTS = [
    {"title": "Just Ralph It", "url": "https://justralph.it"},
    {"title": "Ralph Wiggum as a software engineer", "url": "https://ghuntley.com/ralph"},
]


def respond(status_code: int, body: object = None, *, text: str | None = None) -> httpx.Response:
    request = httpx.Request("POST", "https://search.test/context")
    if text is not None:
        return httpx.Response(status_code, text=text, request=request)
    if body is None:
        return httpx.Response(status_code, request=request)
    return httpx.Response(status_code, json=body, request=request)


class FakeProvider:
    def __init__(self, outcome: httpx.Response | httpx.HTTPError) -> None:
        self.outcome = outcome
        self.calls: list[tuple[dict[str, str], dict[str, str]]] = []

    def post(self, _url: str, **options: object) -> httpx.Response:
        self.calls.append((cast("dict[str, str]", options["json"]), cast("dict[str, str]", options["headers"])))
        if isinstance(self.outcome, httpx.HTTPError):
            raise self.outcome
        return self.outcome
