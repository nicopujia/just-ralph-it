"""Tests for the spec tool wrapper."""

import asyncio
from pathlib import Path

import pytest

from jri.core.tools.markdown_write import MarkdownWriteError
from jri.core.tools.spec import write_spec


def test_spec_writes_markdown_under_specs_directory(tmp_path: Path) -> None:
    """Spec writes are scoped to .jri/specs."""
    result = asyncio.run(
        write_spec(
            project_root=tmp_path,
            path="product",
            content="# Product\n",
        )
    )

    spec_path = tmp_path / ".jri" / "specs" / "product.md"
    assert spec_path.read_text() == "# Product\n"
    assert "product.md" in result
    assert "10 bytes" in result


def test_spec_patches_markdown_under_specs_directory(tmp_path: Path) -> None:
    """Spec patches can make focused edits under .jri/specs."""
    spec_path = tmp_path / ".jri" / "specs" / "product.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("# Product\nold\n", encoding="utf-8")

    result = asyncio.run(
        write_spec(
            project_root=tmp_path,
            path="product",
            patch_text=(
                "*** Begin Patch\n"
                "*** Update File: product.md\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch"
            ),
        )
    )

    assert spec_path.read_text(encoding="utf-8") == "# Product\nnew\n"
    assert "M specs/product.md" in result


def test_spec_requires_content_or_patch_text(tmp_path: Path) -> None:
    """Spec writes require exactly one mutation payload."""
    with pytest.raises(MarkdownWriteError, match="content or patch_text"):
        asyncio.run(write_spec(project_root=tmp_path, path="product"))

    with pytest.raises(MarkdownWriteError, match="content or patch_text"):
        asyncio.run(
            write_spec(
                project_root=tmp_path,
                path="product",
                content="# Product\n",
                patch_text="*** Begin Patch\n*** End Patch",
            )
        )


@pytest.mark.parametrize("path_kind", ["absolute", "traversal"])
def test_spec_rejects_paths_outside_specs_directory(
    tmp_path: Path,
    path_kind: str,
) -> None:
    """Spec paths cannot leave .jri/specs."""
    path = str(tmp_path / "escape.md")
    if path_kind == "traversal":
        path = "../escape.md"

    with pytest.raises(MarkdownWriteError):
        asyncio.run(
            write_spec(
                project_root=tmp_path,
                path=path,
                content="# Escape\n",
            )
        )


def test_spec_rejects_patch_targets_outside_requested_spec(
    tmp_path: Path,
) -> None:
    """Spec patches cannot mutate a different file than requested."""
    (tmp_path / ".jri" / "specs").mkdir(parents=True)

    with pytest.raises(MarkdownWriteError, match="does not target"):
        asyncio.run(
            write_spec(
                project_root=tmp_path,
                path="product",
                patch_text=(
                    "*** Begin Patch\n"
                    "*** Add File: other.md\n"
                    "+# Other\n"
                    "*** End Patch"
                ),
            )
        )
