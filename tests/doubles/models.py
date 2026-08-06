from typing import Any

import httpx
import pytest

from jri.lib.models import ENDPOINT, read_context_limit

CONTEXT_LIMIT = 400_000
CATALOG: dict[str, Any] = {"test": {"limit": {"context": CONTEXT_LIMIT}}}


def serve_catalog(monkeypatch: pytest.MonkeyPatch, catalog: object = CATALOG, *, status_code: int = 200) -> None:
    request = httpx.Request("GET", ENDPOINT)
    serve_outcome(monkeypatch, httpx.Response(status_code, json=catalog, request=request))


def serve_outcome(monkeypatch: pytest.MonkeyPatch, outcome: httpx.Response | httpx.HTTPError) -> None:
    def get(_url: str, **_options: object) -> httpx.Response:
        if isinstance(outcome, httpx.HTTPError):
            raise outcome
        return outcome

    monkeypatch.setattr(httpx, "get", get)
    read_context_limit.cache_clear()
