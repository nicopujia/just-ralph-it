import base64
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from jri.lib.providers import codex

DISTANT_FUTURE = 4_102_444_800
ORIGINATOR = "test-app"


def write_login(path: Path, tokens: dict[str, Any] | None, *, auth_mode: str = "chatgpt") -> None:
    data: dict[str, Any] = {"auth_mode": auth_mode}
    if tokens is not None:
        data["tokens"] = tokens
    (path / "auth.json").write_text(json.dumps(data))


def build_token(expires: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expires}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def respond(status_code: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status_code, json=body, request=httpx.Request("POST", codex.Auth.OAUTH_URL))


def build_client(path: Path, monkeypatch: pytest.MonkeyPatch, requests: list[httpx.Request]) -> codex.Client:
    write_login(
        path, {"access_token": build_token(DISTANT_FUTURE), "refresh_token": "refresh", "account_id": "account"}
    )

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(codex, "DefaultHttpxClient", lambda **options: httpx.Client(transport=transport, **options))
    return codex.Client(ORIGINATOR)


class FakeProvider:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str]]] = []

    def post(self, url: str, **options: object) -> httpx.Response:
        self.calls.append((url, cast("dict[str, str]", options["data"])))
        return self.response
