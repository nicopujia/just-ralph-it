"""Thin wrapper for the Brave LLM Context API.

https://api.search.brave.com/res/v1/llm/context
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Literal, cast

import httpx

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["Error", "Freshness", "GroundingData", "LLMContext", "SearchResult", "SourceMetadata", "ThresholdMode"]


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

    @classmethod
    def from_raw(cls, raw: object) -> SearchResult:
        item = cast("dict[str, object]", raw)
        return cls(
            url=cast("str", item.get("url", "")),
            title=cast("str", item.get("title", "")),
            snippets=cast("list[str]", item.get("snippets", [])),
        )


@dataclass
class SourceMetadata:
    """Metadata for a source URL."""

    url: str
    title: str
    hostname: str
    age: list[str] | None = None

    @classmethod
    def from_raw(cls, url: str, raw: object) -> SourceMetadata:
        item = cast("dict[str, object]", raw)
        return cls(
            url=url,
            title=cast("str", item.get("title", "")),
            hostname=cast("str", item.get("hostname", "")),
            age=(cast("list[str]", item["age"]) if item.get("age") is not None else None),
        )


@dataclass
class GroundingData:
    """Parsed grounding data from the API response."""

    generic: list[SearchResult] = field(default_factory=list)
    poi: SearchResult | None = None
    map: list[SearchResult] = field(default_factory=list)
    sources: dict[str, SourceMetadata] = field(default_factory=dict)


# ── Error ───────────────────────────────────────────────────────────


class Error(Exception):
    """Raised when the Brave API returns an error."""

    status_code: int | None

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ── Client ──────────────────────────────────────────────────────────


class LLMContext:
    """Thin wrapper around the Brave LLM Context API.

    Usage:
        client = LLMContext(api_key="...")
        results = client.search("tallest mountains in the world")
        for r in results.generic:
            print(r.title, r.url)
    """

    _ENDPOINT: ClassVar[str] = "https://api.search.brave.com/res/v1/llm/context"
    _api_key: str
    _timeout: float

    def __init__(self, api_key: str, *, timeout: float = 30.0) -> None:
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
        max_urls: int = 20,
        max_tokens: int = 8192,
        max_snippets: int = 50,
        max_tokens_per_url: int = 4096,
        max_snippets_per_url: int = 50,
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
            max_urls: Max URLs in response (1-50).
            max_tokens: Approx max tokens
                (1024-32768).
            max_snippets: Max snippets across all
                URLs.
            max_tokens_per_url: Max tokens per URL.
            max_snippets_per_url: Max snippets per
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
        body: dict[str, object] = {
            key: value
            for key, value in {
                "query": query,
                "country": country,
                "search_lang": search_lang,
                "count": count,
                "freshness": freshness,
                "maximum_number_of_urls": max_urls,
                "maximum_number_of_tokens": max_tokens,
                "maximum_number_of_snippets": max_snippets,
                "maximum_number_of_tokens_per_url": max_tokens_per_url,
                "maximum_number_of_snippets_per_url": max_snippets_per_url,
                "context_threshold_mode": context_threshold_mode,
                "enable_local": enable_local,
                "goggles": goggles,
            }.items()
            if value is not None
        }
        headers: dict[str, str] = {"Accept": "application/json", "X-Subscription-Token": self._api_key}
        headers.update({
            header: str(value)
            for header, value in {
                "X-Loc-Lat": latitude,
                "X-Loc-Long": longitude,
                "X-Loc-City": loc_city,
                "X-Loc-State": loc_state,
                "X-Loc-State-Name": loc_state_name,
                "X-Loc-Country": loc_country,
                "X-Loc-Postal-Code": loc_postal_code,
            }.items()
            if value is not None
        })
        return self._parse(self._request(body, headers))

    def _request(self, body: dict[str, object], headers: dict[str, str]) -> dict[str, object]:
        """Make the HTTP POST request.

        Returns:
            Parsed JSON response body.

        Raises:
            Error: On HTTP errors or connection failures.
        """
        try:
            response = httpx.post(self._ENDPOINT, json=body, headers=headers, timeout=self._timeout)
            response.raise_for_status()
            return cast("dict[str, object]", response.json())
        except httpx.HTTPError as exc:
            detail = str(exc)
            status_code: int | None = None
            if isinstance(exc, httpx.HTTPStatusError):
                status_code = exc.response.status_code
                try:
                    detail = str(cast("dict[str, object]", exc.response.json()).get("error", detail))
                except (TypeError, ValueError):
                    detail = exc.response.text or detail
            raise Error(detail, status_code=status_code) from exc

    @staticmethod
    def _parse(raw: dict[str, object]) -> GroundingData:
        """Parse the API response into domain models.

        Returns:
            GroundingData with extracted content and sources.
        """
        grounding = cast("dict[str, object]", raw.get("grounding", {}))
        poi_raw = grounding.get("poi")
        sources = cast("dict[str, object]", raw.get("sources", {}))

        return GroundingData(
            generic=[SearchResult.from_raw(item) for item in cast("list[object]", grounding.get("generic", []))],
            poi=(SearchResult.from_raw(poi_raw) if isinstance(poi_raw, dict) else None),
            map=[SearchResult.from_raw(item) for item in cast("list[object]", grounding.get("map", []))],
            sources={url: SourceMetadata.from_raw(url, item) for url, item in sources.items()},
        )
