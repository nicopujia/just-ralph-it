"""Scratchpad note tool wrapper."""

from pathlib import Path

from .write import (
    PatchResult,
    patch_file,
    write_file,
)


async def write_note(
    *,
    project_root: Path,
    patch_text: str,
) -> str:
    """Patch the interviewer scratchpad."""
    allowed_root = project_root / ".jri"
    target_path = project_root / ".jri" / "scratchpad.md"
    result = await patch_file(
        allowed_root=allowed_root,
        target_path=target_path,
        patch_text=patch_text,
    )
    return _format_patch_result(
        project_root=project_root,
        result=result,
    )


async def replace_note(
    *,
    project_root: Path,
    content: str,
) -> str:
    """Create or replace the interviewer scratchpad."""
    allowed_root = project_root / ".jri"
    target_path = project_root / ".jri" / "scratchpad.md"
    result = await write_file(
        allowed_root=allowed_root,
        target_path=target_path,
        content=content or "",
    )
    return f"{result.path.name} written ({result.bytes_written} bytes)"


def _format_patch_result(
    *,
    project_root: Path,
    result: PatchResult,
) -> str:
    prefixes = {"add": "A", "update": "M", "delete": "D"}
    lines = ["Applied patch sequentially:"]
    for change in result.applied:
        relative_path = change.path.relative_to(project_root / ".jri")
        lines.append(f"{prefixes[change.operation]} {relative_path}")
    return "\n".join(lines)
