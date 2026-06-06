"""Tests for the note tool wrapper."""

import asyncio
import inspect
from pathlib import Path

from jri.core.tools.note import replace_note, write_note


def test_note_replaces_scratchpad(tmp_path: Path) -> None:
    """Note replacements target only the project scratchpad."""
    result = asyncio.run(
        replace_note(
            project_root=tmp_path,
            content="# Scratchpad\n\n## Pending Questions\n",
        )
    )

    scratchpad = tmp_path / ".jri" / "scratchpad.md"
    assert scratchpad.read_text() == "# Scratchpad\n\n## Pending Questions\n"
    assert "scratchpad.md" in result
    assert "35 bytes" in result


def test_note_adds_scratchpad_with_patch_text(tmp_path: Path) -> None:
    """Note tool calls create the scratchpad through patch_text."""
    result = asyncio.run(
        write_note(
            project_root=tmp_path,
            patch_text=(
                "*** Begin Patch\n"
                "*** Add File: scratchpad.md\n"
                "+# Scratchpad\n"
                "*** End Patch"
            ),
        )
    )

    scratchpad = tmp_path / ".jri" / "scratchpad.md"
    assert scratchpad.read_text() == "# Scratchpad\n"
    assert "A scratchpad.md" in result


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


def test_note_tool_accepts_only_patch_text_payload() -> None:
    """The model-facing note tool requires patch_text."""
    signature = inspect.signature(write_note)

    assert "content" not in signature.parameters
    assert "patch_text: str" in str(signature)
    assert "patch_text: str =" not in str(signature)
