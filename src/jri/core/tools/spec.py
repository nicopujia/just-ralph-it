"""Specification tool wrapper."""

from pathlib import Path

from .write import (
    PatchResult,
    patch_file,
    write_file,
)


async def write_spec(
    *,
    project_root: Path,
    path: str,
    patch_text: str,
) -> str:
    """Patch a specification Markdown file."""
    target = Path(path)
    if target.suffix != ".md":
        target = target.with_suffix(".md")

    allowed_root = project_root / ".jri" / "specs"
    target_path = allowed_root / target
    result = await patch_file(
        allowed_root=allowed_root,
        target_path=target_path,
        patch_text=patch_text,
    )
    return _format_patch_result(
        project_root=project_root,
        result=result,
    )


async def replace_spec(
    *,
    project_root: Path,
    path: str,
    content: str,
) -> str:
    """Create or replace a specification Markdown file."""
    target = Path(path)
    if target.suffix != ".md":
        target = target.with_suffix(".md")

    allowed_root = project_root / ".jri" / "specs"
    target_path = allowed_root / target
    result = await write_file(
        allowed_root=allowed_root,
        target_path=target_path,
        content=content or "",
    )
    relative_path = result.path.relative_to(project_root / ".jri")
    return f"{relative_path} written ({result.bytes_written} bytes)"


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
