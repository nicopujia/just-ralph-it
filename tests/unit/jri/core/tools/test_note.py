"""Tests for the note tool wrapper."""

import asyncio
from pathlib import Path

import pytest

from jri.core.tools.note import write_note
from jri.core.tools.write import WriteError


def test_note_writes_scratchpad(tmp_path: Path) -> None:
    """Note writes replace only the project scratchpad."""
    result = asyncio.run(
        write_note(
            project_root=tmp_path,
            content="# Scratchpad\n\n## Pending Questions\n",
        )
    )

    scratchpad = tmp_path / ".jri" / "scratchpad.md"
    assert scratchpad.read_text() == "# Scratchpad\n\n## Pending Questions\n"
    assert "scratchpad.md" in result
    assert "35 bytes" in result


def test_note_patches_scratchpad(tmp_path: Path) -> None:
    """Note patches can make focused edits to the scratchpad."""
    scratchpad = tmp_path / ".jri" / "scratchpad.md"
    scratchpad.parent.mkdir(parents=True)
    scratchpad.write_text("# Scratchpad\nold\n", encoding="utf-8")

    result = asyncio.run(
        write_note(
            project_root=tmp_path,
            patch_text=(
                "*** Begin Patch\n"
                "*** Update File: scratchpad.md\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch"
            ),
        )
    )

    assert scratchpad.read_text(encoding="utf-8") == "# Scratchpad\nnew\n"
    assert "M scratchpad.md" in result


def test_note_requires_content_or_patch_text(tmp_path: Path) -> None:
    """Note writes require exactly one mutation payload."""
    with pytest.raises(WriteError, match="content or patch_text"):
        asyncio.run(write_note(project_root=tmp_path))

    with pytest.raises(WriteError, match="content or patch_text"):
        asyncio.run(
            write_note(
                project_root=tmp_path,
                content="# Scratchpad\n",
                patch_text="*** Begin Patch\n*** End Patch",
            )
        )
