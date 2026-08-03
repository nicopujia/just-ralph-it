from typing import Any

import httpx
import pytest

from jri.lib.models import get_context_limit

CONTEXT_LIMIT = 400_000
CATALOG: dict[str, Any] = {"test": {"limit": {"context": CONTEXT_LIMIT}}}


def serve_catalog(monkeypatch: pytest.MonkeyPatch, catalog: object = CATALOG, *, status_code: int = 200) -> None:
    """Serve a models.dev catalog instead of the real service."""

    def get(url: str, **_: object) -> httpx.Response:
        return httpx.Response(status_code, json=catalog, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    get_context_limit.cache_clear()
