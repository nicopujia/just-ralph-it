"""HTTP client test doubles."""

from typing import Self


class FakeClient:
    """Fake httpx async client."""

    def __init__(self, *, timeout: float, follow_redirects: bool) -> None:
        self.timeout: float = timeout
        self.follow_redirects: bool = follow_redirects

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        _ = (exc_type, exc, traceback)

    async def get(self, url: str) -> "FakeResponse":
        """Return a fake response."""
        _ = url
        return FakeResponse()


class FakeResponse:
    """Fake HTTP response."""

    status_code: int = 200
    url: str = "https://example.com/final"
    headers: dict[str, str] = {"content-type": "text/plain"}
    text: str = "x" * 25_000


class FakeBraveClient:
    """Fake Brave Search HTTP client."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.params: dict[str, str | int] = {}

    def create(
        self, *, timeout: float, follow_redirects: bool
    ) -> "FakeBraveClient":
        """Create a fake async client."""
        _ = (timeout, follow_redirects)
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        _ = (exc_type, exc, traceback)

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str | int],
    ) -> "FakeBraveSearchResponse":
        """Record the request and return fake results."""
        assert url == "https://api.search.brave.com/res/v1/web/search"
        self.headers = headers
        self.params = params
        return FakeBraveSearchResponse()


class FakeBraveSearchResponse:
    """Fake Brave Search response."""

    status_code: int = 200

    def raise_for_status(self) -> None:
        """Succeed."""

    def json(self) -> dict[str, object]:
        """Return a minimal Brave Search payload."""
        return {
            "web": {
                "results": [
                    {
                        "title": "Python CLI docs",
                        "url": "https://example.com/python-cli",
                        "description": "Build command line tools in Python.",
                    },
                    {
                        "title": "argparse tutorial",
                        "url": "https://example.com/argparse",
                        "description": "argparse helps parse CLI flags.",
                    },
                ]
            }
        }


class PayloadBraveClient:
    """Fake Brave client with configurable payload."""

    def __init__(self, payload: object) -> None:
        self.headers: dict[str, str] = {}
        self.params: dict[str, str | int] = {}
        self.payload: object = payload

    def create(
        self, *, timeout: float, follow_redirects: bool
    ) -> "PayloadBraveClient":
        """Create a fake async client."""
        _ = (timeout, follow_redirects)
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        _ = (exc_type, exc, traceback)

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str | int],
    ) -> "PayloadBraveResponse":
        """Record the request and return configured results."""
        assert url == "https://api.search.brave.com/res/v1/web/search"
        self.headers = headers
        self.params = params
        return PayloadBraveResponse(self.payload)


class PayloadBraveResponse:
    """Fake Brave Search response with configurable JSON."""

    status_code: int = 200

    def __init__(self, payload: object) -> None:
        self.payload: object = payload

    def raise_for_status(self) -> None:
        """Succeed."""

    def json(self) -> object:
        """Return configured payload."""
        return self.payload
