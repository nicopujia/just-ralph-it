from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx
import pytest


def serve_pages(monkeypatch: pytest.MonkeyPatch, handle: Callable[[httpx.Request], httpx.Response]) -> None:
    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(httpx, "stream", client.stream)


def serve_chunks(monkeypatch: pytest.MonkeyPatch, chunks: list[bytes], served: list[bytes]) -> None:
    @contextmanager
    def stream(_method: str, url: str, **_options: object) -> Iterator[httpx.Response]:
        def serve() -> Iterator[bytes]:
            for chunk in chunks:
                served.append(chunk)
                yield chunk

        yield httpx.Response(200, content=serve(), request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "stream", stream)
