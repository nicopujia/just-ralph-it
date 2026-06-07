# pyright: reportUnannotatedClassAttribute=false
"""Tests for read-only exploration."""

import asyncio
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Self

import pytest

from jri.core.tools.explore import (
    BraveSearchError,
    BraveSearchOptions,
    explore_context,
    fetch_url,
    glob_paths,
    grep_text,
    read_text,
    search_web,
)
from tests.doubles.explorers import RecordingExplorer
from tests.doubles.http import (
    FakeBraveClient,
    PayloadBraveClient,
)

FORMERLY_BLOCKED_FILES = [
    (".env", "TOKEN_DOTENV=secret"),
    (".env.local", "TOKEN_ENV_PREFIX=secret"),
    (".envrc", "TOKEN_ENVRC=secret"),
    (".netrc", "machine example login user password secret"),
    (".npmrc", "//registry/:_authToken=secret"),
    (".pypirc", "password = secret"),
    (".ssh/id_dsa", "TOKEN_DSA=secret"),
    (".ssh/id_ecdsa", "TOKEN_ECDSA=secret"),
    (".ssh/id_ed25519", "TOKEN_ED25519=secret"),
    (".ssh/id_rsa", "TOKEN_RSA=secret"),
    ("cert.key", "TOKEN_KEY=secret"),
    ("cert.pem", "TOKEN_PEM=secret"),
    ("bundle.p12", "TOKEN_P12=secret"),
    ("bundle.pfx", "TOKEN_PFX=secret"),
    (".jri/logs/interview.jsonl", '{"TOKEN_LOG":"secret"}'),
]


def test_explore_invokes_explorer_with_plain_language_request(
    tmp_path: Path,
) -> None:
    """Explore delegates the request to the explorer subagent."""
    explorer = RecordingExplorer("Summary:\n- Found the CLI entrypoint.")

    result = asyncio.run(
        explore_context(
            project_root=tmp_path,
            request="Find the CLI entrypoint.",
            explorer=explorer,
        )
    )

    assert result == "Summary:\n- Found the CLI entrypoint."
    assert explorer.requests == [(tmp_path, "Find the CLI entrypoint.")]


def test_explorer_file_tools_are_read_only(tmp_path: Path) -> None:
    """Explorer helpers read existing project files without mutation."""
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('hi')\n")

    assert glob_paths(pattern="**/*.py", root=tmp_path) == ["src/app.py"]
    assert read_text(path=source, root=tmp_path) == "1: print('hi')"
    assert source.read_text() == "print('hi')\n"


@pytest.mark.parametrize(("relative_path", "contents"), FORMERLY_BLOCKED_FILES)
def test_read_text_allows_formerly_blocked_project_files(
    tmp_path: Path,
    relative_path: str,
    contents: str,
) -> None:
    """Explorer reads any in-project file requested by path."""
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{contents}\n")

    assert read_text(path=target, root=tmp_path) == f"1: {contents}"


def test_grep_text_allows_formerly_blocked_project_files(
    tmp_path: Path,
) -> None:
    """Explorer grep searches any in-project text file."""
    for relative_path, contents in FORMERLY_BLOCKED_FILES:
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{contents}\n")

    result = grep_text(pattern="secret", root=tmp_path, limit=100)

    for relative_path, contents in FORMERLY_BLOCKED_FILES:
        assert f"{relative_path}:1: {contents}" in result


def test_glob_paths_allows_formerly_blocked_project_files(
    tmp_path: Path,
) -> None:
    """Explorer glob returns any in-project file path."""
    for relative_path, contents in FORMERLY_BLOCKED_FILES:
        target = tmp_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{contents}\n")

    result = set(glob_paths(pattern="**/*", root=tmp_path, limit=100))

    for relative_path, _contents in FORMERLY_BLOCKED_FILES:
        assert relative_path in result


def test_read_text_rejects_paths_outside_project_root(
    tmp_path: Path,
) -> None:
    """Explorer reads stay inside the project root."""
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside\n")

    with pytest.raises(ValueError, match="project root"):
        read_text(path=outside, root=tmp_path)


def test_grep_text_skips_matches_outside_project_root(
    tmp_path: Path,
) -> None:
    """Explorer grep does not follow traversal patterns outside the root."""
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret\n")

    assert grep_text(pattern="secret", root=project, include="../*.txt") == ""


def test_grep_text_limits_matches_and_skips_binary_files(
    tmp_path: Path,
) -> None:
    """Grep returns bounded text matches from readable files."""
    source = tmp_path / "src" / "app.py"
    binary = tmp_path / "src" / "image.bin"
    source.parent.mkdir()
    source.write_text("alpha\nalpha again\nbeta\n")
    binary.write_bytes(b"\xff\xfe")

    assert grep_text(pattern="alpha", root=tmp_path, limit=1) == (
        "src/app.py:1: alpha"
    )
    assert grep_text(pattern="missing", root=tmp_path) == ""


def test_web_search_uses_brave_search_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web search returns compact Brave Search results."""
    fake_client = FakeBraveClient()
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        fake_client.create,
    )

    result = asyncio.run(
        search_web(
            query="python cli",
            api_key="brave-key",
            options=BraveSearchOptions(
                count=2,
                country="US",
                search_lang="en",
            ),
        )
    )

    assert fake_client.headers["X-Subscription-Token"] == "brave-key"
    assert fake_client.params == {
        "q": "python cli",
        "count": 2,
        "country": "US",
        "search_lang": "en",
        "result_filter": "web",
    }
    assert result == (
        "Search results:\n"
        "1. Python CLI docs\n"
        "   URL: https://example.com/python-cli\n"
        "   Snippet: Build command line tools in Python.\n"
        "2. argparse tutorial\n"
        "   URL: https://example.com/argparse\n"
        "   Snippet: argparse helps parse CLI flags."
    )


def test_web_search_requires_brave_search_api_key() -> None:
    """Web search fails clearly without Brave credentials."""
    with pytest.raises(BraveSearchError, match="BRAVE_SEARCH_API_KEY"):
        asyncio.run(search_web(query="python cli", api_key=None))


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"web": None},
        {"web": {"results": {}}},
        {"web": {"results": [None]}},
    ],
)
def test_web_search_returns_no_results_for_empty_brave_payloads(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    """Web search handles empty Brave payload variants."""
    fake_client = PayloadBraveClient(payload)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        fake_client.create,
    )

    result = asyncio.run(search_web(query="python cli", api_key="brave-key"))

    assert result == "No web results found."


def test_web_search_uses_fallbacks_for_missing_result_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web search formats malformed individual results safely."""
    empty_result: dict[str, object] = {}
    payload: dict[str, object] = {"web": {"results": [empty_result]}}
    fake_client = PayloadBraveClient(payload)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        fake_client.create,
    )

    result = asyncio.run(search_web(query="python cli", api_key="brave-key"))

    assert result == (
        "Search results:\n1. Untitled result\n   URL: unknown\n   Snippet: "
    )


def test_fetch_url_returns_bounded_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URL fetching reports status, final URL, type, and bounded body."""
    monkeypatch.setattr(socket, "getaddrinfo", public_address_info)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        PublicLargeBodyClient,
    )

    result = asyncio.run(fetch_url("https://example.com"))

    assert "Status: 200" in result
    assert "Final URL: https://example.com/final" in result
    assert "Content-Type: text/plain" in result
    assert len(result) < 20_200


def test_fetch_url_stops_reading_response_body_at_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curl stops downloading once the response body cap is reached."""
    monkeypatch.setattr(socket, "getaddrinfo", public_address_info)
    CountingLargeBodyClient.last_response = None
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        CountingLargeBodyClient,
    )

    result = asyncio.run(fetch_url("https://example.com/large"))

    response = CountingLargeBodyClient.last_response
    assert response is not None
    assert result.endswith(f"\n\n{'x' * 20_000}")
    assert response.bytes_read == 20_000


def test_fetch_url_allows_public_ip_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curl allows public IP targets through the normal fetch path."""
    monkeypatch.setattr(socket, "getaddrinfo", public_address_info)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        PublicLargeBodyClient,
    )

    result = asyncio.run(fetch_url("https://93.184.216.34"))

    assert "Status: 200" in result
    assert "Final URL: https://example.com/final" in result


def test_fetch_url_rejects_hostnames_resolving_to_private_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curl resolves hostnames and rejects private network addresses."""

    def private_address_info(
        host: str,
        port: int | str | None,
        **kwargs: int,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        _ = (host, port, kwargs)
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.1", 443),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", private_address_info)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        FailingFetchClient,
    )

    with pytest.raises(ValueError, match="private network"):
        asyncio.run(fetch_url("https://metadata.example"))


def test_fetch_url_rejects_connected_private_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curl rejects private peer addresses before returning content."""
    monkeypatch.setattr(socket, "getaddrinfo", public_address_info)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        ConnectedPrivateClient,
    )

    with pytest.raises(ValueError, match="private network"):
        asyncio.run(fetch_url("https://example.com"))


def test_fetch_url_allows_connected_public_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curl allows public peer addresses through the normal fetch path."""
    monkeypatch.setattr(socket, "getaddrinfo", public_address_info)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        ConnectedPublicClient,
    )

    result = asyncio.run(fetch_url("https://example.com"))

    assert "Status: 200" in result
    assert result.endswith("\n\npublic body")


def test_fetch_url_rejects_unresolvable_hostnames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curl rejects hostnames that DNS cannot resolve."""
    monkeypatch.setattr(socket, "getaddrinfo", unresolvable_address_info)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        FailingFetchClient,
    )

    with pytest.raises(ValueError, match="resolve"):
        asyncio.run(fetch_url("https://missing.example"))


def test_fetch_url_rejects_hostnames_without_resolved_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curl rejects empty DNS answers before fetching."""
    monkeypatch.setattr(socket, "getaddrinfo", empty_address_info)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        FailingFetchClient,
    )

    with pytest.raises(ValueError, match="resolve"):
        asyncio.run(fetch_url("https://empty.example"))


def test_fetch_url_follows_public_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curl follows redirects when each target remains public."""
    monkeypatch.setattr(socket, "getaddrinfo", public_address_info)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        PublicRedirectClient,
    )

    result = asyncio.run(fetch_url("https://example.com/start"))

    assert "Status: 200" in result
    assert "Final URL: https://example.com/final" in result
    assert result.endswith("\n\npublic body")


def test_fetch_url_rejects_redirects_to_private_hostnames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curl resolves redirected hostnames before following them."""
    monkeypatch.setattr(socket, "getaddrinfo", mixed_address_info)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        RedirectToPrivateHostnameClient,
    )

    with pytest.raises(ValueError, match="private network"):
        asyncio.run(fetch_url("https://example.com"))


def test_fetch_url_returns_redirect_response_without_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curl returns redirect responses that do not provide a Location."""
    monkeypatch.setattr(socket, "getaddrinfo", public_address_info)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        RedirectWithoutLocationClient,
    )

    result = asyncio.run(fetch_url("https://example.com/start"))

    assert "Status: 302" in result
    assert "Final URL: https://example.com/start" in result


def test_fetch_url_limits_public_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curl rejects excessive public redirect chains."""
    monkeypatch.setattr(socket, "getaddrinfo", public_address_info)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        LoopingRedirectClient,
    )

    with pytest.raises(ValueError, match="maximum redirects"):
        asyncio.run(fetch_url("https://example.com/start"))


def test_fetch_url_rejects_private_network_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Curl rejects responses redirected to private network targets."""
    monkeypatch.setattr(socket, "getaddrinfo", public_address_info)
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        RedirectToPrivateClient,
    )

    with pytest.raises(ValueError, match="private network"):
        asyncio.run(fetch_url("https://example.com"))


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://127.0.0.1",
        "http://[::1]",
        "http://169.254.169.254",
        "http://10.0.0.1",
        "http://172.16.0.1",
        "http://192.168.1.10",
    ],
)
def test_fetch_url_rejects_direct_private_network_targets(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    """Curl rejects private network targets before making a request."""
    monkeypatch.setattr(
        "jri.core.tools.explore.httpx.AsyncClient",
        FailingFetchClient,
    )

    with pytest.raises(ValueError, match="private network"):
        asyncio.run(fetch_url(url))


@dataclass(frozen=True)
class FetchResponse:
    """Fake HTTP response for scripted fetch tests."""

    status_code: int
    url: str
    headers: dict[str, str]
    text: str
    encoding: str = "utf-8"
    extensions: dict[str, object] = field(default_factory=dict)

    async def aiter_bytes(
        self,
        chunk_size: int | None = None,
    ) -> AsyncIterator[bytes]:
        """Yield response body bytes."""
        body = self.text.encode(self.encoding)
        size = len(body) if chunk_size is None else max(chunk_size, 1)
        for index in range(0, len(body), size):
            yield body[index : index + size]


class FetchResponseStream:
    """Async context manager for scripted fetch responses."""

    def __init__(self, response: FetchResponse) -> None:
        self._response = response

    async def __aenter__(self) -> FetchResponse:
        return self._response

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        _ = (exc_type, exc, traceback)


def public_address_info(
    host: str,
    port: int | str | None,
    **kwargs: int,
) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """Resolve any test hostname to a public address."""
    _ = (host, port, kwargs)
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            ("93.184.216.34", 443),
        )
    ]


def mixed_address_info(
    host: str,
    port: int | str | None,
    **kwargs: int,
) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """Resolve one test hostname privately and all others publicly."""
    if host == "metadata.example":
        _ = (port, kwargs)
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("10.0.0.1", 443),
            )
        ]
    return public_address_info(host, port, **kwargs)


def unresolvable_address_info(
    host: str,
    port: int | str | None,
    **kwargs: int,
) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """Fail DNS resolution for test hostnames."""
    _ = (host, port, kwargs)
    raise socket.gaierror(socket.EAI_NONAME, "not known")


def empty_address_info(
    host: str,
    port: int | str | None,
    **kwargs: int,
) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """Resolve test hostnames to no addresses."""
    _ = (host, port, kwargs)
    return []


class FakeNetworkStream:
    """Fake httpcore network stream exposing a peer address."""

    def __init__(self, server_address: tuple[str, int]) -> None:
        self._server_address = server_address

    def get_extra_info(self, info: str) -> tuple[str, int] | None:
        """Return the fake peer address."""
        if info == "server_addr":
            return self._server_address
        return None


class ScriptedFetchClient:
    """Fake client that returns preconfigured responses."""

    responses: ClassVar[list[FetchResponse]] = []
    expected_urls: ClassVar[list[str]] = []
    expected_follow_redirects: ClassVar[bool | None] = False

    def __init__(self, *, timeout: float, follow_redirects: bool) -> None:
        _ = timeout
        if type(self).expected_follow_redirects is not None:
            assert follow_redirects is type(self).expected_follow_redirects
        self._responses = list(type(self).responses)
        self._expected_urls = list(type(self).expected_urls)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        _ = (exc_type, exc, traceback)

    async def get(self, url: str) -> FetchResponse:
        """Return the next fake response."""
        return self._next_response(url)

    def stream(self, method: str, url: str) -> FetchResponseStream:
        """Return the next fake response as a stream."""
        assert method == "GET"
        return FetchResponseStream(self._next_response(url))

    def _next_response(self, url: str) -> FetchResponse:
        if self._expected_urls:
            assert url == self._expected_urls.pop(0)
        if not self._responses:
            pytest.fail(f"unexpected fetch of {url}")
        return self._responses.pop(0)


class CountingLargeBodyResponse:
    """Fake streaming response that tracks consumed bytes."""

    status_code: int = 200
    url: str = "https://example.com/large"
    headers: dict[str, str] = {"content-type": "text/plain"}
    encoding: str = "utf-8"
    extensions: dict[str, object] = {}

    def __init__(self) -> None:
        self._body = b"x" * 25_000
        self.bytes_read = 0

    @property
    def text(self) -> str:
        """Expose a fully buffered body like httpx.Response.text."""
        self.bytes_read = len(self._body)
        return self._body.decode(self.encoding)

    async def aiter_bytes(
        self,
        chunk_size: int | None = None,
    ) -> AsyncIterator[bytes]:
        """Yield body bytes and count how much the caller consumes."""
        size = len(self._body) if chunk_size is None else max(chunk_size, 1)
        while self.bytes_read < len(self._body):
            end = min(self.bytes_read + size, len(self._body))
            chunk = self._body[self.bytes_read : end]
            self.bytes_read = end
            yield chunk


class CountingLargeBodyStream:
    """Async context manager for the counting response."""

    def __init__(self, response: CountingLargeBodyResponse) -> None:
        self._response = response

    async def __aenter__(self) -> CountingLargeBodyResponse:
        return self._response

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        _ = (exc_type, exc, traceback)


class CountingLargeBodyClient:
    """Fake client that exposes both current and streaming fetch APIs."""

    last_response: ClassVar[CountingLargeBodyResponse | None] = None

    def __init__(self, *, timeout: float, follow_redirects: bool) -> None:
        _ = timeout
        assert follow_redirects is False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        _ = (exc_type, exc, traceback)

    async def get(self, url: str) -> CountingLargeBodyResponse:
        """Return a response whose text property buffers the full body."""
        assert url == "https://example.com/large"
        response = CountingLargeBodyResponse()
        type(self).last_response = response
        return response

    def stream(
        self,
        method: str,
        url: str,
    ) -> CountingLargeBodyStream:
        """Return a stream response for the large test body."""
        assert method == "GET"
        assert url == "https://example.com/large"
        response = CountingLargeBodyResponse()
        type(self).last_response = response
        return CountingLargeBodyStream(response)


class PublicLargeBodyClient(ScriptedFetchClient):
    """Fake client that returns a large public response."""

    responses: ClassVar[list[FetchResponse]] = [
        FetchResponse(
            status_code=200,
            url="https://example.com/final",
            headers={"content-type": "text/plain"},
            text="x" * 25_000,
        )
    ]


class FailingFetchClient(ScriptedFetchClient):
    """Fake client that fails if a request is made."""


class RedirectToPrivateClient(ScriptedFetchClient):
    """Fake client that fails if a private redirect is fetched."""

    expected_urls: ClassVar[list[str]] = ["https://example.com"]
    responses: ClassVar[list[FetchResponse]] = [
        FetchResponse(
            status_code=302,
            url="https://example.com",
            headers={
                "content-type": "text/plain",
                "location": "http://169.254.169.254/latest/meta-data",
            },
            text="redirecting",
        )
    ]


class RedirectToPrivateHostnameClient(ScriptedFetchClient):
    """Fake client that redirects to a hostname with private DNS."""

    expected_urls: ClassVar[list[str]] = ["https://example.com"]
    responses: ClassVar[list[FetchResponse]] = [
        FetchResponse(
            status_code=302,
            url="https://example.com",
            headers={
                "content-type": "text/plain",
                "location": "https://metadata.example/latest",
            },
            text="redirecting",
        )
    ]


class ConnectedPrivateClient(ScriptedFetchClient):
    """Fake client connected to a private peer address."""

    expected_urls: ClassVar[list[str]] = ["https://example.com"]
    responses: ClassVar[list[FetchResponse]] = [
        FetchResponse(
            status_code=200,
            url="https://example.com",
            headers={"content-type": "text/plain"},
            text="private body",
            extensions={
                "network_stream": FakeNetworkStream(("10.0.0.1", 443))
            },
        )
    ]


class ConnectedPublicClient(ScriptedFetchClient):
    """Fake client connected to a public peer address."""

    expected_urls: ClassVar[list[str]] = ["https://example.com"]
    responses: ClassVar[list[FetchResponse]] = [
        FetchResponse(
            status_code=200,
            url="https://example.com",
            headers={"content-type": "text/plain"},
            text="public body",
            extensions={
                "network_stream": FakeNetworkStream(("93.184.216.34", 443))
            },
        )
    ]


class PublicRedirectClient(ScriptedFetchClient):
    """Fake client that simulates a public redirect chain."""

    expected_urls: ClassVar[list[str]] = [
        "https://example.com/start",
        "https://example.com/final",
    ]
    responses: ClassVar[list[FetchResponse]] = [
        FetchResponse(
            status_code=302,
            url="https://example.com/start",
            headers={"content-type": "text/plain", "location": "/final"},
            text="redirecting",
        ),
        FetchResponse(
            status_code=200,
            url="https://example.com/final",
            headers={"content-type": "text/plain"},
            text="public body",
        ),
    ]


class RedirectWithoutLocationClient(ScriptedFetchClient):
    """Fake client that returns a redirect without a Location."""

    expected_urls: ClassVar[list[str]] = ["https://example.com/start"]
    responses: ClassVar[list[FetchResponse]] = [
        FetchResponse(
            status_code=302,
            url="https://example.com/start",
            headers={"content-type": "text/plain"},
            text="redirect without location",
        )
    ]


class LoopingRedirectClient(ScriptedFetchClient):
    """Fake client that always redirects to another public URL."""

    responses: ClassVar[list[FetchResponse]] = [
        FetchResponse(
            status_code=302,
            url="https://example.com/start",
            headers={"content-type": "text/plain", "location": "/start"},
            text="redirect again",
        )
    ] * 21
