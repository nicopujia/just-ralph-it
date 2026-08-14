import base64
import json
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from jri.lib.providers import codex

DISTANT_FUTURE = 4_102_444_800
NOW = 1_800_000_000
ORIGINATOR = "test-app"


def write_login(path: Path, tokens: object, *, auth_mode: str = "chatgpt") -> None:
    data: dict[str, Any] = {"auth_mode": auth_mode}
    if tokens is not None:
        data["tokens"] = tokens
    (path / "auth.json").write_text(json.dumps(data))


def build_token(expires: int) -> str:
    return encode_token(json.dumps({"exp": expires}))


def encode_token(payload: str) -> str:
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def respond(status_code: int, body: "dict[str, Any] | str") -> httpx.Response:
    request = httpx.Request("POST", codex.Auth.OAUTH_URL)
    if isinstance(body, str):
        return httpx.Response(status_code, text=body, request=request)
    return httpx.Response(status_code, json=body, request=request)


def retry_after_rejection(auth: codex.Auth, on_first_request: Callable[[], None]) -> None:
    # This drives the httpx retry handshake by hand. A failure comes out unwrapped.
    # `on_first_request` is a stand-in for a parallel process. It writes `auth.json` again during the first attempt.
    flow = auth.sync_auth_flow(httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses"))
    request = next(flow)
    on_first_request()
    with suppress(StopIteration):
        flow.send(httpx.Response(httpx.codes.UNAUTHORIZED, request=request))


def deny_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    def replace(_path: Path, _target: Path) -> Path:
        raise OSError("read-only file system")

    monkeypatch.setattr(codex.Path, "replace", replace)


def build_client(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requests: list[httpx.Request],
    *,
    statuses: Sequence[int] = (),
    on_first_request: Callable[[], None] | None = None,
) -> codex.Client:
    write_login(
        path, {"access_token": build_token(DISTANT_FUTURE), "refresh_token": "refresh", "account_id": "account"}
    )

    def handle(request: httpx.Request) -> httpx.Response:
        # A retry sends the same request object with new headers. Make a copy before the next attempt
        # changes it.
        requests.append(httpx.Request(request.method, request.url, headers=request.headers, content=request.content))
        if len(requests) == 1 and on_first_request is not None:
            on_first_request()
        status = statuses[len(requests) - 1] if len(requests) <= len(statuses) else httpx.codes.OK
        return httpx.Response(status, json={})

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(codex, "DefaultHttpxClient", lambda **options: httpx.Client(transport=transport, **options))
    return codex.Client(ORIGINATOR)


class FakeProvider:
    def __init__(self, response: "httpx.Response | Exception") -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str]]] = []

    def post(self, url: str, **options: object) -> httpx.Response:
        self.calls.append((url, cast("dict[str, str]", options["data"])))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FrozenClock:
    @staticmethod
    def now(tz: tzinfo) -> datetime:
        return datetime.fromtimestamp(NOW, tz=tz)
