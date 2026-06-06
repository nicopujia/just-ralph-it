"""Pydantic AI explorer tool adapters."""

import os
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import RunContext, Tool

from jri.core.tools.explore import (
    BraveSearchOptions,
    fetch_url,
    glob_paths,
    grep_text,
    read_text,
    search_web,
)


@dataclass(frozen=True)
class ExplorerDeps:
    """Dependencies for the explorer agent."""

    project_root: Path


def build_explorer_tools() -> list[Tool[ExplorerDeps]]:
    """Build stable explorer tool registrations."""
    return [
        Tool(glob_tool, takes_ctx=True, name="glob"),
        Tool(grep_tool, takes_ctx=True, name="grep"),
        Tool(read_tool, takes_ctx=True, name="read"),
        Tool(curl_tool, takes_ctx=False, name="curl"),
        Tool(web_search_tool, takes_ctx=False, name="web_search"),
    ]


def glob_tool(
    ctx: RunContext[ExplorerDeps],
    pattern: str,
    limit: int = 100,
) -> str:
    """Find files by glob pattern under the project root."""
    return "\n".join(
        glob_paths(pattern=pattern, root=ctx.deps.project_root, limit=limit)
    )


def grep_tool(
    ctx: RunContext[ExplorerDeps],
    pattern: str,
    include: str = "**/*",
    limit: int = 50,
) -> str:
    """Search local file contents under the project root."""
    return grep_text(
        pattern=pattern,
        root=ctx.deps.project_root,
        include=include,
        limit=limit,
    )


def read_tool(
    ctx: RunContext[ExplorerDeps],
    path: str,
    offset: int = 0,
    limit: int = 80,
) -> str:
    """Read bounded text from a local file."""
    requested = Path(path)
    target = (
        requested if requested.is_absolute() else ctx.deps.project_root / path
    )
    return read_text(
        path=target,
        root=ctx.deps.project_root,
        offset=offset,
        limit=limit,
    )


async def curl_tool(url: str) -> str:
    """Fetch a specific URL with bounded output."""
    return await fetch_url(url)


async def web_search_tool(
    query: str,
    count: int = 5,
    country: str = "US",
    search_lang: str = "en",
) -> str:
    """Search the web through Brave Search."""
    return await search_web(
        query=query,
        api_key=os.environ.get("BRAVE_SEARCH_API_KEY"),
        options=BraveSearchOptions(
            count=count,
            country=country,
            search_lang=search_lang,
        ),
    )
