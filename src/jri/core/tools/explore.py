"""Read-only exploration tool wrapper."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import httpx

BRAVE_WEB_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_JRI_LOG_PATH_PREFIX = (".jri", "logs")


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
        and not _is_log_path(relative)
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
        if relative is None or _is_log_path(relative):
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
    async with httpx.AsyncClient(
        timeout=request_timeout,
        follow_redirects=True,
    ) as client:
        response = await client.get(url)
    content_type = cast(
        "str",
        response.headers.get("content-type", "unknown"),
    )
    body = response.text[:20_000]
    return (
        f"Status: {response.status_code}\n"
        f"Final URL: {response.url}\n"
        f"Content-Type: {content_type}\n\n"
        f"{body}"
    )


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
    if _is_log_path(relative):
        msg = "Explorer file tools do not expose .jri logs."
        raise ValueError(msg)
    return resolved_path


def _project_relative(path: Path, resolved_root: Path) -> Path | None:
    try:
        return path.resolve().relative_to(resolved_root)
    except ValueError:
        return None


def _is_log_path(relative: Path) -> bool:
    # Logs are telemetry. Durable memory lives in specs and notes.
    return (
        len(relative.parts) >= len(_JRI_LOG_PATH_PREFIX)
        and relative.parts[: len(_JRI_LOG_PATH_PREFIX)] == _JRI_LOG_PATH_PREFIX
    )
