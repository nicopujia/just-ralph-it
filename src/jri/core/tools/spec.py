"""Specification tool wrapper."""

from pathlib import Path

from .write import (
    PatchResult,
    WriteError,
    patch_file,
    write_file,
)


async def write_spec(
    *,
    project_root: Path,
    path: str,
    content: str | None = None,
    patch_text: str | None = None,
) -> str:
    """Create, replace, or patch a specification Markdown file."""
    target = Path(path)
    if target.suffix != ".md":
        target = target.with_suffix(".md")

    _validate_payload(content=content, patch_text=patch_text)
    allowed_root = project_root / ".jri" / "specs"
    target_path = allowed_root / target
    if patch_text is not None:
        result = await patch_file(
            allowed_root=allowed_root,
            target_path=target_path,
            patch_text=patch_text,
        )
        return _format_patch_result(
            project_root=project_root,
            result=result,
        )

    result = await write_file(
        allowed_root=allowed_root,
        target_path=target_path,
        content=content or "",
    )
    relative_path = result.path.relative_to(project_root / ".jri")
    return f"{relative_path} written ({result.bytes_written} bytes)"


def _validate_payload(
    *,
    content: str | None,
    patch_text: str | None,
) -> None:
    if (content is None) == (patch_text is None):
        msg = "Provide exactly one of content or patch_text."
        raise WriteError(msg)


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
