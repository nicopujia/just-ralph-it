from typing import Any

import httpx
import pytest

from jri.lib.models import ENDPOINT, read_context_limit

CONTEXT_LIMIT = 400_000
CATALOG: dict[str, Any] = {"test": {"limit": {"context": CONTEXT_LIMIT}}}


def serve_catalog(monkeypatch: pytest.MonkeyPatch, catalog: object = CATALOG, *, status_code: int = 200) -> None:
    serve_outcome(monkeypatch, build_response(catalog, status_code=status_code))


def build_response(catalog: object = CATALOG, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=catalog, request=httpx.Request("GET", ENDPOINT))


def serve_outcome(monkeypatch: pytest.MonkeyPatch, *outcomes: httpx.Response | httpx.HTTPError) -> None:
    remaining = list(outcomes)

    # Check this test support.
    # Check this test support.
    def get(_url: str, **_options: object) -> httpx.Response:
        outcome = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(outcome, httpx.HTTPError):
            raise outcome
        return outcome

    monkeypatch.setattr(httpx, "get", get)
    read_context_limit.cache_clear()
