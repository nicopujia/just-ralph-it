"""Specification tool wrapper."""

import re
from pathlib import Path

from jri.core.readiness import check_mvp_readiness

from .write import (
    PatchResult,
    patch_file,
    write_file,
)

_HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_FENCE_PATTERN = re.compile(r"^\s*(```|~~~)")
_EMPTY_READINESS_MISSING = check_mvp_readiness("").missing


class SpecValidationError(ValueError):
    """Raised when model-facing spec content has the wrong shape."""


def validate_spec_markdown(content: str) -> None:
    """Validate model-facing spec content before persisting it."""
    reason = _find_spec_markdown_problem(content)
    if reason is not None:
        raise SpecValidationError(reason)


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


def _find_spec_markdown_problem(content: str) -> str | None:
    markdown = content.strip()
    if not markdown:
        return "content is empty"

    lines = list(_iter_non_code_lines(markdown))
    first_content = next((line for line in lines if line.strip()), "")
    if not _HEADING_PATTERN.match(first_content):
        return "content must start with a Markdown heading"
    if not any(_is_body_line(line) for line in lines):
        return "content must include requirement text under a heading"
    if check_mvp_readiness(markdown).missing == _EMPTY_READINESS_MISSING:
        return "content must include at least one spec readiness section"
    return None


def _iter_non_code_lines(markdown: str) -> list[str]:
    lines: list[str] = []
    inside_fence = False
    for line in markdown.splitlines():
        if _FENCE_PATTERN.match(line):
            inside_fence = not inside_fence
            continue
        if not inside_fence:
            lines.append(line)
    return lines


def _is_body_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not _HEADING_PATTERN.match(stripped)
