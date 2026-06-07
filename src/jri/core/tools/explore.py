"""Read-only exploration tool wrapper."""

import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urljoin, urlsplit

import httpx

BRAVE_WEB_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_FETCH_REDIRECT_LIMIT = 20
_FETCH_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_FETCH_BODY_BYTE_LIMIT = 20_000
_FETCH_BODY_CHARACTER_LIMIT = 20_000


@dataclass(frozen=True)
class BraveSearchOptions:
    """Brave Web Search request options."""

    count: int = 5
    country: str = "US"
    search_lang: str = "en"
    request_timeout: float = 10.0


class ContextExplorer(Protocol):
    """Subagent interface used for read-only exploration."""

    async def run(self, *, project_root: Path, request: str) -> str:
        """Run an exploration request."""
        ...


class _ExtraInfoProvider(Protocol):
    """Network stream exposing socket metadata."""

    def get_extra_info(self, info: str) -> object:
        """Return socket metadata."""
        ...


async def explore_context(
    *,
    project_root: Path,
    request: str,
    explorer: ContextExplorer,
) -> str:
    """Ask the explorer subagent for compact context."""
    return await explorer.run(project_root=project_root, request=request)


def glob_paths(*, pattern: str, root: Path, limit: int = 100) -> list[str]:
    """Return sorted paths matching a glob under a root."""
    resolved_root = root.resolve()
    paths = [
        relative.as_posix()
        for path in root.glob(pattern)
        if path.is_file()
        and (relative := _project_relative(path, resolved_root)) is not None
    ]
    return sorted(paths)[:limit]


def read_text(
    *, path: Path, root: Path, offset: int = 0, limit: int = 80
) -> str:
    """Read bounded text with line numbers."""
    resolved_path = _resolve_project_file(path=path, root=root)
    lines = resolved_path.read_text(encoding="utf-8").splitlines()
    selected = lines[offset : offset + limit]
    return "\n".join(
        f"{line_number}: {line}"
        for line_number, line in enumerate(selected, start=offset + 1)
    )


def grep_text(
    *,
    pattern: str,
    root: Path,
    include: str = "**/*",
    limit: int = 50,
) -> str:
    """Search local text files under a root."""
    regex = re.compile(pattern)
    matches: list[str] = []
    resolved_root = root.resolve()
    for path in sorted(root.glob(include)):
        if not path.is_file():
            continue
        relative = _project_relative(path, resolved_root)
        if relative is None:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if regex.search(line):
                matches.append(
                    f"{relative.as_posix()}:{line_number}: {line[:200]}"
                )
                if len(matches) >= limit:
                    return "\n".join(matches)
    return "\n".join(matches)


async def fetch_url(url: str, *, request_timeout: float = 10.0) -> str:
    """Fetch bounded text context for a URL."""
    current_url = url
    async with httpx.AsyncClient(
        timeout=request_timeout,
        follow_redirects=False,
    ) as client:
        for _ in range(_FETCH_REDIRECT_LIMIT + 1):
            _reject_private_network_url(current_url)
            async with client.stream("GET", current_url) as response:
                _reject_private_network_response(response)
                next_url = _redirect_url(response)
                if next_url is None:
                    body = await _read_limited_response_text(response)
                    return _format_fetch_response(response, body)
                current_url = next_url

    msg = "Explorer curl exceeded maximum redirects."
    raise ValueError(msg)


async def _read_limited_response_text(response: httpx.Response) -> str:
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=_FETCH_BODY_BYTE_LIMIT):
        remaining = _FETCH_BODY_BYTE_LIMIT - len(body)
        body.extend(chunk[:remaining])
        if len(body) >= _FETCH_BODY_BYTE_LIMIT:
            break
    text = bytes(body).decode(response.encoding or "utf-8", errors="replace")
    return text[:_FETCH_BODY_CHARACTER_LIMIT]


def _format_fetch_response(response: httpx.Response, body: str) -> str:
    _reject_private_network_response(response)
    content_type = cast(
        "str",
        response.headers.get("content-type", "unknown"),
    )
    return (
        f"Status: {response.status_code}\n"
        f"Final URL: {response.url}\n"
        f"Content-Type: {content_type}\n\n"
        f"{body}"
    )


def _redirect_url(response: httpx.Response) -> str | None:
    if response.status_code not in _FETCH_REDIRECT_STATUS_CODES:
        return None
    location = cast("str | None", response.headers.get("location"))
    if location is None:
        return None
    next_url = urljoin(str(response.url), location)
    _reject_private_network_url(next_url)
    return next_url


def _reject_private_network_url(url: object) -> None:
    host = urlsplit(str(url)).hostname or ""
    if _is_loopback_hostname(host):
        msg = "Explorer curl does not allow private network targets."
        raise ValueError(msg)
    for address in _host_addresses(host):
        if _is_private_network_address(address):
            msg = "Explorer curl does not allow private network targets."
            raise ValueError(msg)


def _host_addresses(host: str) -> tuple[IPv4Address | IPv6Address, ...]:
    try:
        return (ip_address(host),)
    except ValueError:
        return _resolve_host_addresses(host)


def _resolve_host_addresses(
    host: str,
) -> tuple[IPv4Address | IPv6Address, ...]:
    try:
        address_info = socket.getaddrinfo(
            host,
            None,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        msg = "Explorer curl could not resolve target host."
        raise ValueError(msg) from exc

    addresses: list[IPv4Address | IPv6Address] = []
    for info in address_info:
        socket_address = info[4]
        address_text = cast("str", socket_address[0])
        addresses.append(ip_address(address_text))

    if not addresses:
        msg = "Explorer curl could not resolve target host."
        raise ValueError(msg)
    return tuple(addresses)


def _is_private_network_address(
    address: IPv4Address | IPv6Address,
) -> bool:
    return address.is_loopback or address.is_link_local or address.is_private


def _reject_private_network_response(response: httpx.Response) -> None:
    _reject_private_network_url(response.url)
    connected_address = _connected_address(response)
    if connected_address is None:
        return
    if _is_private_network_address(connected_address):
        msg = "Explorer curl does not allow private network targets."
        raise ValueError(msg)


def _connected_address(
    response: httpx.Response,
) -> IPv4Address | IPv6Address | None:
    extensions = cast(
        "Mapping[str, object]",
        getattr(response, "extensions", {}),
    )
    network_stream = extensions.get("network_stream")
    if network_stream is None:
        return None
    extra_info_provider = cast("_ExtraInfoProvider", network_stream)
    server_address = cast(
        "tuple[str, int]",
        extra_info_provider.get_extra_info("server_addr"),
    )
    return ip_address(server_address[0])


def _is_loopback_hostname(host: str) -> bool:
    return host.rstrip(".").lower().split(".")[-1] == "localhost"


class BraveSearchError(RuntimeError):
    """Raised when Brave Search cannot run."""


async def search_web(
    *,
    query: str,
    api_key: str | None,
    options: BraveSearchOptions | None = None,
) -> str:
    """Search the web through Brave Search."""
    if not api_key:
        msg = "BRAVE_SEARCH_API_KEY is required for web_search."
        raise BraveSearchError(msg)

    search_options = options or BraveSearchOptions()
    params: dict[str, str | int] = {
        "q": query,
        "count": max(1, min(search_options.count, 20)),
        "country": search_options.country,
        "search_lang": search_options.search_lang,
        "result_filter": "web",
    }
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    async with httpx.AsyncClient(
        timeout=search_options.request_timeout,
        follow_redirects=True,
    ) as client:
        response = await client.get(
            BRAVE_WEB_SEARCH_URL,
            headers=headers,
            params=params,
        )
    response.raise_for_status()
    payload = cast("object", response.json())
    return _format_brave_results(payload)


def _format_brave_results(payload: object) -> str:
    results = _extract_brave_results(payload)
    if not results:
        return "No web results found."

    rendered = ["Search results:"]
    for index, result in enumerate(results, start=1):
        rendered.extend([
            f"{index}. {_field(result, 'title', 'Untitled result')}",
            f"   URL: {_field(result, 'url', 'unknown')}",
            f"   Snippet: {_field(result, 'description', '')}",
        ])
    return "\n".join(rendered)


def _extract_brave_results(payload: object) -> list[Mapping[str, object]]:
    payload_mapping = _object_mapping(payload)
    if payload_mapping is None:
        return []
    web = _object_mapping(payload_mapping.get("web"))
    if web is None:
        return []
    results = web.get("results")
    if not isinstance(results, list):
        return []
    result_items = cast("list[object]", results)
    return [
        result
        for item in result_items
        if (result := _object_mapping(item)) is not None
    ]


def _object_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast("Mapping[str, object]", value)


def _field(
    result: Mapping[str, object],
    name: str,
    fallback: str,
) -> str:
    value = result.get(name)
    if not isinstance(value, str):
        return fallback
    return " ".join(value.split())


def _resolve_project_file(*, path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    relative = _project_relative(resolved_path, resolved_root)
    if relative is None:
        msg = "Explorer file paths must stay inside the project root."
        raise ValueError(msg)
    return resolved_path


def _project_relative(path: Path, resolved_root: Path) -> Path | None:
    try:
        return path.resolve().relative_to(resolved_root)
    except ValueError:
        return None
