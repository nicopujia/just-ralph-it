"""Tests for the spec tool wrapper."""

import asyncio
import inspect
from pathlib import Path

import pytest

from jri.core.tools.spec import replace_spec, write_spec
from jri.core.tools.write import WriteError


def test_spec_replaces_markdown_under_specs_directory(tmp_path: Path) -> None:
    """Spec replacements are scoped to .jri/specs."""
    result = asyncio.run(
        replace_spec(
            project_root=tmp_path,
            path="product.md",
            content="# Product\n",
        )
    )

    spec_path = tmp_path / ".jri" / "specs" / "product.md"
    assert spec_path.read_text() == "# Product\n"
    assert "product.md" in result
    assert "10 bytes" in result


def test_spec_adds_markdown_with_patch_text(tmp_path: Path) -> None:
    """Spec tool calls create files through patch_text."""
    result = asyncio.run(
        write_spec(
            project_root=tmp_path,
            path="product",
            patch_text=(
                "*** Begin Patch\n"
                "*** Add File: product.md\n"
                "+# Product\n"
                "*** End Patch"
            ),
        )
    )

    spec_path = tmp_path / ".jri" / "specs" / "product.md"
    assert spec_path.read_text() == "# Product\n"
    assert "A specs/product.md" in result


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


def test_spec_tool_accepts_only_patch_text_payload() -> None:
    """The model-facing spec tool requires patch_text."""
    signature = inspect.signature(write_spec)

    assert "content" not in signature.parameters
    assert "patch_text: str" in str(signature)
    assert "patch_text: str =" not in str(signature)


@pytest.mark.parametrize("path_kind", ["absolute", "traversal"])
def test_spec_rejects_paths_outside_specs_directory(
    tmp_path: Path,
    path_kind: str,
) -> None:
    """Spec paths cannot leave .jri/specs."""
    path = str(tmp_path / "escape.md")
    if path_kind == "traversal":
        path = "../escape.md"

    with pytest.raises(WriteError):
        asyncio.run(
            write_spec(
                project_root=tmp_path,
                path=path,
                patch_text=(
                    "*** Begin Patch\n"
                    f"*** Add File: {path}\n"
                    "+# Escape\n"
                    "*** End Patch"
                ),
            )
        )


def test_spec_rejects_patch_targets_outside_requested_spec(
    tmp_path: Path,
) -> None:
    """Spec patches cannot mutate a different file than requested."""
    (tmp_path / ".jri" / "specs").mkdir(parents=True)

    with pytest.raises(WriteError, match="does not target"):
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
