"""Scratchpad note tool wrapper."""

from pathlib import Path

from .markdown_write import (
    MarkdownPatchResult,
    MarkdownWriteError,
    patch_markdown_file,
    write_markdown_file,
)


async def write_note(
    *,
    project_root: Path,
    content: str | None = None,
    patch_text: str | None = None,
) -> str:
    """Create, replace, or patch the interviewer scratchpad."""
    _validate_payload(content=content, patch_text=patch_text)
    allowed_root = project_root / ".jri"
    target_path = project_root / ".jri" / "scratchpad.md"
    if patch_text is not None:
        result = await patch_markdown_file(
            allowed_root=allowed_root,
            target_path=target_path,
            patch_text=patch_text,
        )
        return _format_patch_result(
            project_root=project_root,
            result=result,
        )

    result = await write_markdown_file(
        allowed_root=allowed_root,
        target_path=target_path,
        content=content or "",
    )
    return f"{result.path.name} written ({result.bytes_written} bytes)"


def _validate_payload(
    *,
    content: str | None,
    patch_text: str | None,
) -> None:
    if (content is None) == (patch_text is None):
        msg = "Provide exactly one of content or patch_text."
        raise MarkdownWriteError(msg)


def _format_patch_result(
    *,
    project_root: Path,
    result: MarkdownPatchResult,
) -> str:
    prefixes = {"add": "A", "update": "M", "delete": "D"}
    lines = ["Applied patch sequentially:"]
    for change in result.applied:
        relative_path = change.path.relative_to(project_root / ".jri")
        lines.append(f"{prefixes[change.operation]} {relative_path}")
    return "\n".join(lines)
