"""Thin wrapper for the Brave LLM Context API.

https://api.search.brave.com/res/v1/llm/context
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Literal, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from collections.abc import Sequence
    from http.client import HTTPResponse

__all__ = [
    "BraveError",
    "BraveLLMContext",
    "Freshness",
    "GroundingData",
    "SearchResult",
    "SourceMetadata",
    "ThresholdMode",
]

# ── Types ───────────────────────────────────────────────────────────

ThresholdMode = Literal["strict", "balanced", "lenient", "disabled"]
Freshness = Literal["pd", "pw", "pm", "py"]

# ── Response models ─────────────────────────────────────────────────


@dataclass
class SearchResult:
    """A single result from the grounding data."""

    url: str
    title: str
    snippets: list[str]


@dataclass
class SourceMetadata:
    """Metadata for a source URL."""

    url: str
    title: str
    hostname: str
    age: list[str] | None = None


@dataclass
class GroundingData:
    """Parsed grounding data from the API response."""

    generic: list[SearchResult] = field(default_factory=list)
    poi: SearchResult | None = None
    map: list[SearchResult] = field(default_factory=list)
    sources: dict[str, SourceMetadata] = field(default_factory=dict)


# ── Error ───────────────────────────────────────────────────────────


class BraveError(Exception):
    """Raised when the Brave API returns an error."""

    status_code: int | None

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code


# ── Helpers ─────────────────────────────────────────────────────────


def _as_dict(value: object) -> dict[str, object]:
    """Cast a value to ``dict[str, object]``.

    Returns:
        The value cast to a dict.
    """
    return cast("dict[str, object]", value)


def _as_list(value: object) -> list[object]:
    """Cast a value to ``list[object]``.

    Returns:
        The value cast to a list.
    """
    return cast("list[object]", value)


def _as_str(value: object) -> str:
    """Cast a value to ``str``.

    Returns:
        The value cast to a string.
    """
    return cast("str", value)


def _as_str_list(value: object) -> list[str]:
    """Cast a value to ``list[str]``.

    Returns:
        The value cast to a list of strings.
    """
    return cast("list[str]", value)


# ── Client ──────────────────────────────────────────────────────────


class BraveLLMContext:
    """Thin wrapper around the Brave LLM Context API.

    Usage:
        client = BraveLLMContext(api_key="...")
        results = client.search("tallest mountains in the world")
        for r in results.generic:
            print(r.title, r.url)
    """

    _ENDPOINT: ClassVar[str] = (
        "https://api.search.brave.com/res/v1/llm/context"
    )
    _api_key: str
    _timeout: float

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout

    # ── Public API ──────────────────────────────────────────────

    def search(  # noqa: PLR0913
        self,
        query: str,
        *,
        country: str = "us",
        search_lang: str = "en",
        count: int = 20,
        freshness: Freshness | str | None = None,
        maximum_number_of_urls: int = 20,
        maximum_number_of_tokens: int = 8192,
        maximum_number_of_snippets: int = 50,
        maximum_number_of_tokens_per_url: int = 4096,
        maximum_number_of_snippets_per_url: int = 50,
        context_threshold_mode: ThresholdMode = "balanced",
        enable_local: bool | None = None,
        goggles: str | Sequence[str] | None = None,
        # Location headers
        latitude: float | None = None,
        longitude: float | None = None,
        loc_city: str | None = None,
        loc_state: str | None = None,
        loc_state_name: str | None = None,
        loc_country: str | None = None,
        loc_postal_code: str | None = None,
    ) -> GroundingData:
        """Search the web and return extracted content for LLM use.

        Args:
            query: Search query (1-400 chars, max 50 words).
            country: 2-char country code for search results.
            search_lang: Language preference for results.
            count: Max number of search results to consider (1-50).
            freshness: Filter by page age (pd/pw/pm/py or
                custom range).
            maximum_number_of_urls: Max URLs in response (1-50).
            maximum_number_of_tokens: Approx max tokens
                (1024-32768).
            maximum_number_of_snippets: Max snippets across all
                URLs.
            maximum_number_of_tokens_per_url: Max tokens per URL.
            maximum_number_of_snippets_per_url: Max snippets per
                URL.
            context_threshold_mode: Relevance threshold mode.
            enable_local: Force local recall on/off (None = auto).
            goggles: Goggle URL or inline definition for
                re-ranking.
            latitude: Latitude for location-aware queries.
            longitude: Longitude for location-aware queries.
            loc_city: City name for location-aware queries.
            loc_state: State/region code (ISO 3166-2).
            loc_state_name: State/region name.
            loc_country: 2-letter country code.
            loc_postal_code: Postal code.

        Returns:
            Parsed grounding data with extracted content and
            source metadata.
        """
        body = self._build_body(
            query=query,
            country=country,
            search_lang=search_lang,
            count=count,
            freshness=freshness,
            maximum_number_of_urls=maximum_number_of_urls,
            maximum_number_of_tokens=maximum_number_of_tokens,
            maximum_number_of_snippets=maximum_number_of_snippets,
            maximum_number_of_tokens_per_url=(
                maximum_number_of_tokens_per_url
            ),
            maximum_number_of_snippets_per_url=(
                maximum_number_of_snippets_per_url
            ),
            context_threshold_mode=context_threshold_mode,
            enable_local=enable_local,
            goggles=goggles,
        )
        headers = self._build_headers(
            latitude=latitude,
            longitude=longitude,
            loc_city=loc_city,
            loc_state=loc_state,
            loc_state_name=loc_state_name,
            loc_country=loc_country,
            loc_postal_code=loc_postal_code,
        )
        raw = self._request(body, headers)
        return self._parse(raw)

    # ── Helpers ─────────────────────────────────────────────────

    @staticmethod
    def _build_body(**kwargs: object) -> dict[str, object]:
        """Build the JSON request body.

        Returns:
            Dict with all non-None keyword arguments.
        """
        return {k: v for k, v in kwargs.items() if v is not None}

    def _build_headers(  # noqa: PLR0913
        self,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        loc_city: str | None = None,
        loc_state: str | None = None,
        loc_state_name: str | None = None,
        loc_country: str | None = None,
        loc_postal_code: str | None = None,
    ) -> dict[str, str]:
        """Build request headers including auth and location.

        Returns:
            HTTP headers dict with subscription token and
            optional location values.
        """
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
            "X-Subscription-Token": self._api_key,
        }
        loc_map: dict[str, float | str | None] = {
            "X-Loc-Lat": latitude,
            "X-Loc-Long": longitude,
            "X-Loc-City": loc_city,
            "X-Loc-State": loc_state,
            "X-Loc-State-Name": loc_state_name,
            "X-Loc-Country": loc_country,
            "X-Loc-Postal-Code": loc_postal_code,
        }
        for header, value in loc_map.items():
            if value is not None:
                headers[header] = str(value)
        return headers

    def _request(
        self,
        body: dict[str, object],
        headers: dict[str, str],
    ) -> dict[str, object]:
        """Make the HTTP POST request.

        Returns:
            Parsed JSON response body.

        Raises:
            BraveError: On HTTP errors or connection failures.
        """
        data = json.dumps(body).encode("utf-8")
        req = Request(  # noqa: S310
            self._ENDPOINT,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(req, timeout=self._timeout) as http_resp:  # noqa: S310  # pyright: ignore[reportAny]
                resp = cast("HTTPResponse", http_resp)
                raw = resp.read()
                encoding = resp.headers.get_content_charset(
                    "utf-8",
                )
                return _as_dict(
                    cast(
                        "dict[str, object]",
                        json.loads(raw.decode(encoding)),
                    ),
                )
        except HTTPError as exc:
            try:
                error_body = (
                    exc.read().decode()  # type: ignore[union-attr]
                )
                error_json = cast(
                    "dict[str, object]",
                    json.loads(error_body),
                )
                detail = error_json.get("error", str(exc))
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = str(exc)
            raise BraveError(
                str(detail),
                status_code=exc.code,
            ) from exc
        except URLError as exc:
            raise BraveError(str(exc.reason)) from exc

    @staticmethod
    def _parse(raw: dict[str, object]) -> GroundingData:
        """Parse the API response into domain models.

        Returns:
            GroundingData with extracted content and sources.
        """
        grounding = _as_dict(raw.get("grounding", {}))

        generic = [
            SearchResult(
                url=_as_str(
                    cast("dict[str, object]", item).get("url", ""),
                ),
                title=_as_str(
                    cast("dict[str, object]", item).get(
                        "title",
                        "",
                    ),
                ),
                snippets=_as_str_list(
                    cast("dict[str, object]", item).get(
                        "snippets",
                        [],
                    ),
                ),
            )
            for item in _as_list(grounding.get("generic", []))
        ]

        poi_raw = grounding.get("poi")
        poi = (
            SearchResult(
                url=_as_str(
                    cast("dict[str, object]", poi_raw).get(
                        "url",
                        "",
                    ),
                ),
                title=_as_str(
                    cast("dict[str, object]", poi_raw).get(
                        "title",
                        "",
                    ),
                ),
                snippets=_as_str_list(
                    cast("dict[str, object]", poi_raw).get(
                        "snippets",
                        [],
                    ),
                ),
            )
            if isinstance(poi_raw, dict)
            else None
        )

        map_results = [
            SearchResult(
                url=_as_str(
                    cast("dict[str, object]", item).get("url", ""),
                ),
                title=_as_str(
                    cast("dict[str, object]", item).get(
                        "title",
                        "",
                    ),
                ),
                snippets=_as_str_list(
                    cast("dict[str, object]", item).get(
                        "snippets",
                        [],
                    ),
                ),
            )
            for item in _as_list(grounding.get("map", []))
        ]

        sources: dict[str, SourceMetadata] = {}
        sources_raw = _as_dict(raw.get("sources", {}))
        for url in sources_raw:
            meta_dict = _as_dict(sources_raw[url])
            sources[url] = SourceMetadata(
                url=url,
                title=_as_str(meta_dict.get("title", "")),
                hostname=_as_str(meta_dict.get("hostname", "")),
                age=(
                    _as_str_list(meta_dict["age"])
                    if meta_dict.get("age") is not None
                    else None
                ),
            )

        return GroundingData(
            generic=generic,
            poi=poi,
            map=map_results,
            sources=sources,
        )
