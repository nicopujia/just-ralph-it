"""Tests for scoped Markdown writes used by JRI tools."""

import asyncio
from pathlib import Path

import pytest

from jri.core.tools.markdown_write import (
    MarkdownWriteError,
    patch_markdown_file,
    patch_markdown_files,
    write_markdown_file,
)


def test_markdown_write_creates_parent_directories(tmp_path: Path) -> None:
    """Markdown writes create parents and report bytes written."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = allowed_root / "product" / "overview.md"

    result = asyncio.run(
        write_markdown_file(
            allowed_root=allowed_root,
            target_path=target_path,
            content="# Product\n",
        )
    )

    assert target_path.read_text() == "# Product\n"
    assert result.path == target_path
    assert result.bytes_written == len(b"# Product\n")


def test_markdown_write_rejects_targets_outside_allowed_root(
    tmp_path: Path,
) -> None:
    """Markdown writes cannot escape their allowed root."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = tmp_path / "project" / "README.md"

    with pytest.raises(MarkdownWriteError, match="allowed root"):
        asyncio.run(
            write_markdown_file(
                allowed_root=allowed_root,
                target_path=target_path,
                content="# Escape\n",
            )
        )

    assert not target_path.exists()


def test_markdown_write_serializes_same_file_writes(tmp_path: Path) -> None:
    """Concurrent writes to the same file do not overlap."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = allowed_root / "product.md"
    operations = RecordingOperations()

    async def write_twice() -> None:
        await asyncio.gather(
            write_markdown_file(
                allowed_root=allowed_root,
                target_path=target_path,
                content="first\n",
                operations=operations,
            ),
            write_markdown_file(
                allowed_root=allowed_root,
                target_path=target_path,
                content="second\n",
                operations=operations,
            ),
        )

    asyncio.run(write_twice())

    assert operations.max_active_writes == 1
    assert operations.contents[target_path.resolve()] in {
        "first\n",
        "second\n",
    }


def test_markdown_patch_updates_existing_file(tmp_path: Path) -> None:
    """Markdown patches can apply focused updates."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = allowed_root / "product.md"
    target_path.parent.mkdir(parents=True)
    target_path.write_text("# Product\nold\n", encoding="utf-8")

    result = asyncio.run(
        patch_markdown_files(
            allowed_root=allowed_root,
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

    assert target_path.read_text(encoding="utf-8") == "# Product\nnew\n"
    assert [change.operation for change in result.applied] == ["update"]


def test_markdown_patch_adds_and_deletes_files(tmp_path: Path) -> None:
    """Markdown patches can create and remove scoped files."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    remove_path = allowed_root / "remove.md"
    remove_path.parent.mkdir(parents=True)
    remove_path.write_text("remove\n", encoding="utf-8")

    result = asyncio.run(
        patch_markdown_files(
            allowed_root=allowed_root,
            patch_text=(
                "*** Begin Patch\n"
                "*** Add File: created.md\n"
                "+created\n"
                "*** Delete File: remove.md\n"
                "*** End Patch"
            ),
        )
    )

    assert (allowed_root / "created.md").read_text(encoding="utf-8") == (
        "created\n"
    )
    assert not remove_path.exists()
    assert [change.operation for change in result.applied] == [
        "add",
        "delete",
    ]


def test_markdown_patch_adds_empty_files(tmp_path: Path) -> None:
    """Empty add hunks create empty files without adding a newline."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"

    result = asyncio.run(
        patch_markdown_files(
            allowed_root=allowed_root,
            patch_text=(
                "*** Begin Patch\n*** Add File: empty.md\n*** End Patch"
            ),
        )
    )

    assert (allowed_root / "empty.md").read_text(encoding="utf-8") == ""
    assert result.applied[0].bytes_written == 0


def test_markdown_patch_accepts_absolute_targets_inside_root(
    tmp_path: Path,
) -> None:
    """Absolute patch paths are allowed only inside the allowed root."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = allowed_root / "product.md"
    target_path.parent.mkdir(parents=True)
    target_path.write_text("old\n", encoding="utf-8")

    result = asyncio.run(
        patch_markdown_files(
            allowed_root=allowed_root,
            patch_text=(
                "*** Begin Patch\n"
                f"*** Update File: {target_path}\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch"
            ),
        )
    )

    assert target_path.read_text(encoding="utf-8") == "new\n"
    assert result.applied[0].operation == "update"


def test_markdown_patch_file_rejects_other_targets(tmp_path: Path) -> None:
    """Single-file patches cannot mutate a different target."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"

    with pytest.raises(MarkdownWriteError, match="does not target"):
        asyncio.run(
            patch_markdown_file(
                allowed_root=allowed_root,
                target_path=allowed_root / "product.md",
                patch_text=(
                    "*** Begin Patch\n"
                    "*** Add File: other.md\n"
                    "+# Other\n"
                    "*** End Patch"
                ),
            )
        )


def test_markdown_patch_rejects_escape_before_mutation(
    tmp_path: Path,
) -> None:
    """Patch hunks cannot escape the allowed root."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    escape_path = tmp_path / "project" / "README.md"

    with pytest.raises(MarkdownWriteError, match="allowed root"):
        asyncio.run(
            patch_markdown_files(
                allowed_root=allowed_root,
                patch_text=(
                    "*** Begin Patch\n"
                    "*** Add File: ../README.md\n"
                    "+# Escape\n"
                    "*** End Patch"
                ),
            )
        )

    assert not escape_path.exists()


def test_markdown_patch_validates_all_hunks_before_mutating(
    tmp_path: Path,
) -> None:
    """A later invalid update prevents earlier add hunks."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"

    with pytest.raises(MarkdownWriteError, match=r"missing\.md"):
        asyncio.run(
            patch_markdown_files(
                allowed_root=allowed_root,
                patch_text=(
                    "*** Begin Patch\n"
                    "*** Add File: created.md\n"
                    "+created\n"
                    "*** Update File: missing.md\n"
                    "@@\n"
                    "-before\n"
                    "+after\n"
                    "*** End Patch"
                ),
            )
        )

    assert not (allowed_root / "created.md").exists()


def test_markdown_patch_rejects_invalid_patch_text(tmp_path: Path) -> None:
    """Invalid patch text is surfaced as a Markdown write error."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"

    with pytest.raises(MarkdownWriteError, match="missing Begin/End"):
        asyncio.run(
            patch_markdown_files(
                allowed_root=allowed_root,
                patch_text="not a patch",
            )
        )


def test_markdown_patch_rejects_empty_patch(tmp_path: Path) -> None:
    """Empty patches are invalid."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"

    with pytest.raises(MarkdownWriteError, match="empty patch"):
        asyncio.run(
            patch_markdown_files(
                allowed_root=allowed_root,
                patch_text="*** Begin Patch\n*** End Patch",
            )
        )


def test_markdown_patch_rejects_moves_before_mutation(
    tmp_path: Path,
) -> None:
    """Moves are parsed but not supported by scoped Markdown patches."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = allowed_root / "old.md"
    target_path.parent.mkdir(parents=True)
    target_path.write_text("old\n", encoding="utf-8")

    with pytest.raises(MarkdownWriteError, match="moves"):
        asyncio.run(
            patch_markdown_files(
                allowed_root=allowed_root,
                patch_text=(
                    "*** Begin Patch\n"
                    "*** Update File: old.md\n"
                    "*** Move to: new.md\n"
                    "@@\n"
                    "-old\n"
                    "+new\n"
                    "*** End Patch"
                ),
            )
        )

    assert not (allowed_root / "new.md").exists()
    assert target_path.read_text(encoding="utf-8") == "old\n"


def test_markdown_patch_rejects_add_target_that_exists(
    tmp_path: Path,
) -> None:
    """Add hunks do not replace existing files."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = allowed_root / "product.md"
    target_path.parent.mkdir(parents=True)
    target_path.write_text("sentinel\n", encoding="utf-8")

    with pytest.raises(MarkdownWriteError, match="already exists"):
        asyncio.run(
            patch_markdown_files(
                allowed_root=allowed_root,
                patch_text=(
                    "*** Begin Patch\n"
                    "*** Add File: product.md\n"
                    "+replacement\n"
                    "*** End Patch"
                ),
            )
        )

    assert target_path.read_text(encoding="utf-8") == "sentinel\n"


def test_markdown_patch_rejects_stale_updates(tmp_path: Path) -> None:
    """Updates fail if content changes after patch derivation."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = allowed_root / "product.md"
    operations = StaleUpdateOperations(target_path.resolve(), "before\n")

    with pytest.raises(MarkdownWriteError, match="changed"):
        asyncio.run(
            patch_markdown_files(
                allowed_root=allowed_root,
                patch_text=(
                    "*** Begin Patch\n"
                    "*** Update File: product.md\n"
                    "@@\n"
                    "-before\n"
                    "+after\n"
                    "*** End Patch"
                ),
                operations=operations,
            )
        )

    assert operations.contents[target_path.resolve()] == b"winner\n"


class RecordingOperations:
    """Fake filesystem operations that detect overlapping writes."""

    def __init__(self) -> None:
        self.active_writes: int = 0
        self.max_active_writes: int = 0
        self.contents: dict[Path, str] = {}

    @staticmethod
    async def make_directory(path: Path) -> None:
        """Record directory creation."""
        _ = path

    async def write_text(self, path: Path, content: str) -> None:
        """Record text writes while yielding to concurrent tasks."""
        self.active_writes += 1
        self.max_active_writes = max(
            self.max_active_writes,
            self.active_writes,
        )
        await asyncio.sleep(0)
        self.contents[path] = content
        self.active_writes -= 1


class StaleUpdateOperations:
    """Fake operations that make the second read stale."""

    def __init__(self, path: Path, content: str) -> None:
        self.path: Path = path
        self.contents: dict[Path, bytes] = {
            path: content.encode(),
        }
        self.reads: int = 0

    @staticmethod
    async def make_directory(path: Path) -> None:
        """Record directory creation."""
        _ = path

    async def write_text(self, path: Path, content: str) -> None:
        """Record text writes."""
        self.contents[path] = content.encode()

    async def create_text(self, path: Path, content: str) -> None:
        """Create text only when the file does not already exist."""
        if path in self.contents:
            raise FileExistsError(path)
        self.contents[path] = content.encode()

    async def read_bytes(self, path: Path) -> bytes:
        """Return stale bytes after the first read."""
        self.reads += 1
        if self.reads == 2:
            self.contents[path] = b"winner\n"
        return self.contents[path]

    async def remove_file(self, path: Path) -> None:
        """Remove a file."""
        del self.contents[path]
