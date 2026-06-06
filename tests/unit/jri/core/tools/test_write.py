"""Tests for scoped writes used by JRI tools."""

import asyncio
from pathlib import Path

import pytest

from jri.core.tools.write import (
    AddHunk,
    DeleteHunk,
    UpdateChunk,
    UpdateHunk,
    WriteError,
    derive_update,
    join_bom,
    parse_patch,
    patch_file,
    patch_files,
    write_file,
)


def test_write_creates_parent_directories(tmp_path: Path) -> None:
    """Scoped writes create parents and report bytes written."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = allowed_root / "product" / "overview.md"

    result = asyncio.run(
        write_file(
            allowed_root=allowed_root,
            target_path=target_path,
            content="# Product\n",
        )
    )

    assert target_path.read_text() == "# Product\n"
    assert result.path == target_path
    assert result.bytes_written == len(b"# Product\n")


def test_write_rejects_targets_outside_allowed_root(
    tmp_path: Path,
) -> None:
    """Scoped writes cannot escape their allowed root."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = tmp_path / "project" / "README.md"

    with pytest.raises(WriteError, match="allowed root"):
        asyncio.run(
            write_file(
                allowed_root=allowed_root,
                target_path=target_path,
                content="# Escape\n",
            )
        )

    assert not target_path.exists()


def test_write_serializes_same_file_writes(tmp_path: Path) -> None:
    """Concurrent writes to the same file do not overlap."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = allowed_root / "product.md"
    operations = RecordingOperations()

    async def write_twice() -> None:
        await asyncio.gather(
            write_file(
                allowed_root=allowed_root,
                target_path=target_path,
                content="first\n",
                operations=operations,
            ),
            write_file(
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


def test_patch_updates_existing_file(tmp_path: Path) -> None:
    """Scoped patches can apply focused updates."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = allowed_root / "product.md"
    target_path.parent.mkdir(parents=True)
    target_path.write_text("# Product\nold\n", encoding="utf-8")

    result = asyncio.run(
        patch_files(
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


def test_patch_adds_and_deletes_files(tmp_path: Path) -> None:
    """Scoped patches can create and remove scoped files."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    remove_path = allowed_root / "remove.md"
    remove_path.parent.mkdir(parents=True)
    remove_path.write_text("remove\n", encoding="utf-8")

    result = asyncio.run(
        patch_files(
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


def test_patch_adds_empty_files(tmp_path: Path) -> None:
    """Empty add hunks create empty files without adding a newline."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"

    result = asyncio.run(
        patch_files(
            allowed_root=allowed_root,
            patch_text=(
                "*** Begin Patch\n*** Add File: empty.md\n*** End Patch"
            ),
        )
    )

    assert (allowed_root / "empty.md").read_text(encoding="utf-8") == ""
    assert result.applied[0].bytes_written == 0


def test_patch_accepts_absolute_targets_inside_root(
    tmp_path: Path,
) -> None:
    """Absolute patch paths are allowed only inside the allowed root."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = allowed_root / "product.md"
    target_path.parent.mkdir(parents=True)
    target_path.write_text("old\n", encoding="utf-8")

    result = asyncio.run(
        patch_files(
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


def test_patch_file_rejects_other_targets(tmp_path: Path) -> None:
    """Single-file patches cannot mutate a different target."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"

    with pytest.raises(WriteError, match="does not target"):
        asyncio.run(
            patch_file(
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


def test_patch_rejects_escape_before_mutation(
    tmp_path: Path,
) -> None:
    """Patch hunks cannot escape the allowed root."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    escape_path = tmp_path / "project" / "README.md"

    with pytest.raises(WriteError, match="allowed root"):
        asyncio.run(
            patch_files(
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


def test_patch_validates_all_hunks_before_mutating(
    tmp_path: Path,
) -> None:
    """A later invalid update prevents earlier add hunks."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"

    with pytest.raises(WriteError, match=r"missing\.md"):
        asyncio.run(
            patch_files(
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


def test_patch_rejects_invalid_patch_text(tmp_path: Path) -> None:
    """Invalid patch text is surfaced as a write error."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"

    with pytest.raises(WriteError, match="missing Begin/End"):
        asyncio.run(
            patch_files(
                allowed_root=allowed_root,
                patch_text="not a patch",
            )
        )


def test_patch_rejects_empty_patch(tmp_path: Path) -> None:
    """Empty patches are invalid."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"

    with pytest.raises(WriteError, match="empty patch"):
        asyncio.run(
            patch_files(
                allowed_root=allowed_root,
                patch_text="*** Begin Patch\n*** End Patch",
            )
        )


def test_patch_rejects_moves_before_mutation(
    tmp_path: Path,
) -> None:
    """Moves are parsed but not supported by scoped patches."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = allowed_root / "old.md"
    target_path.parent.mkdir(parents=True)
    target_path.write_text("old\n", encoding="utf-8")

    with pytest.raises(WriteError, match="moves"):
        asyncio.run(
            patch_files(
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


def test_patch_rejects_add_target_that_exists(
    tmp_path: Path,
) -> None:
    """Add hunks do not replace existing files."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = allowed_root / "product.md"
    target_path.parent.mkdir(parents=True)
    target_path.write_text("sentinel\n", encoding="utf-8")

    with pytest.raises(WriteError, match="already exists"):
        asyncio.run(
            patch_files(
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


def test_patch_rejects_existing_add_before_writing_earlier_hunks(
    tmp_path: Path,
) -> None:
    """Later add conflicts are validated before earlier hunks commit."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    first_path = allowed_root / "a.md"
    second_path = allowed_root / "b.md"
    first_path.parent.mkdir(parents=True)
    first_path.write_text("old\n", encoding="utf-8")
    second_path.write_text("sentinel\n", encoding="utf-8")

    with pytest.raises(WriteError, match="already exists"):
        asyncio.run(
            patch_files(
                allowed_root=allowed_root,
                patch_text=(
                    "*** Begin Patch\n"
                    "*** Update File: a.md\n"
                    "@@\n"
                    "-old\n"
                    "+new\n"
                    "*** Add File: b.md\n"
                    "+replacement\n"
                    "*** End Patch"
                ),
            )
        )

    assert first_path.read_text(encoding="utf-8") == "old\n"
    assert second_path.read_text(encoding="utf-8") == "sentinel\n"


def test_patch_rejects_add_race_during_commit(tmp_path: Path) -> None:
    """Add hunks still reject files created after preparation."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"

    with pytest.raises(WriteError, match="already exists"):
        asyncio.run(
            patch_files(
                allowed_root=allowed_root,
                patch_text=(
                    "*** Begin Patch\n"
                    "*** Add File: product.md\n"
                    "+# Product\n"
                    "*** End Patch"
                ),
                operations=RaceAddOperations(),
            )
        )


def test_patch_rejects_stale_updates(tmp_path: Path) -> None:
    """Updates fail if content changes after patch derivation."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = allowed_root / "product.md"
    operations = StaleUpdateOperations(target_path.resolve(), "before\n")

    with pytest.raises(WriteError, match="changed"):
        asyncio.run(
            patch_files(
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


def test_patch_rejects_missing_update_context_as_write_error(
    tmp_path: Path,
) -> None:
    """Patch derivation failures are surfaced as scoped write errors."""
    allowed_root = tmp_path / "project" / ".jri" / "specs"
    target_path = allowed_root / "product.md"
    target_path.parent.mkdir(parents=True)
    target_path.write_text("# Product\nold\n", encoding="utf-8")

    with pytest.raises(WriteError, match="Failed to find context"):
        asyncio.run(
            patch_files(
                allowed_root=allowed_root,
                patch_text=(
                    "*** Begin Patch\n"
                    "*** Update File: product.md\n"
                    "@@ -1,6 +1,6 @@\n"
                    "-old\n"
                    "+new\n"
                    "*** End Patch"
                ),
            )
        )

    assert target_path.read_text(encoding="utf-8") == "# Product\nold\n"


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


class RaceAddOperations:
    """Fake operations that create an add conflict after preparation."""

    @staticmethod
    async def make_directory(path: Path) -> None:
        """Record directory creation."""
        _ = path

    @staticmethod
    async def write_text(path: Path, content: str) -> None:
        """Write text."""
        _ = (path, content)

    @staticmethod
    async def create_text(path: Path, content: str) -> None:
        """Pretend another writer created the file first."""
        _ = content
        raise FileExistsError(path)

    @staticmethod
    async def read_bytes(path: Path) -> bytes:
        """Pass the missing-file preflight."""
        raise FileNotFoundError(path)

    @staticmethod
    async def remove_file(path: Path) -> None:
        """Remove a file."""
        _ = path


def _patch(*lines: str) -> str:
    return "\n".join(lines)


def test_patch_parses_add_update_and_delete_hunks() -> None:
    """Patch text supports add, update, and delete file hunks."""
    assert parse_patch(
        _patch(
            "*** Begin Patch",
            "*** Add File: add.md",
            "+added",
            "*** Update File: update.md",
            "@@ section",
            "-old",
            "+new",
            "*** Delete File: delete.md",
            "*** End Patch",
        )
    ) == (
        AddHunk(path="add.md", contents="added"),
        UpdateHunk(
            path="update.md",
            chunks=(
                UpdateChunk(
                    old_lines=("old",),
                    new_lines=("new",),
                    change_context="section",
                ),
            ),
        ),
        DeleteHunk(path="delete.md"),
    )


def test_patch_strips_heredoc_wrapper() -> None:
    """Shell heredoc wrappers are ignored around the patch body."""
    assert parse_patch(
        _patch(
            "cat <<'PATCH'",
            "*** Begin Patch",
            "*** Add File: add.md",
            "+added",
            "*** End Patch",
            "PATCH",
        )
    ) == (AddHunk(path="add.md", contents="added"),)


def test_patch_derives_fuzzy_line_updates_and_preserves_bom() -> None:
    """Whitespace-tolerant updates retain existing UTF-8 BOM intent."""
    update = derive_update(
        path="update.md",
        chunks=(UpdateChunk(old_lines=("  old   ",), new_lines=("new",)),),
        original="\ufeffold\n",
    )

    assert update.content == "new\n"
    assert update.bom
    assert join_bom(update.content, bom=update.bom) == "\ufeffnew\n"


def test_patch_derives_updates_from_files_without_trailing_newline() -> None:
    """Updates accept source files that do not end in a newline."""
    update = derive_update(
        path="update.md",
        chunks=(UpdateChunk(old_lines=("old",), new_lines=("new",)),),
        original="old",
    )

    assert update.content == "new\n"


def test_patch_matches_eof_anchored_chunks_from_the_end() -> None:
    """EOF-marked chunks match the final occurrence only."""
    update = derive_update(
        path="update.md",
        chunks=(
            UpdateChunk(
                old_lines=("marker", "end"),
                new_lines=("marker changed", "end"),
                end_of_file=True,
            ),
        ),
        original="marker\nmiddle\nmarker\nend\n",
    )

    assert update.content == "marker\nmiddle\nmarker changed\nend\n"


def test_patch_rejects_eof_chunks_that_do_not_match_the_end() -> None:
    """EOF-marked chunks cannot fall back to earlier matching lines."""
    with pytest.raises(ValueError, match="Failed to find expected lines"):
        derive_update(
            path="update.md",
            chunks=(
                UpdateChunk(
                    old_lines=("marker", "middle"),
                    new_lines=("changed", "middle"),
                    end_of_file=True,
                ),
            ),
            original="marker\nmiddle\nmarker\nend\n",
        )


def test_patch_parses_end_of_file_marker_inside_update_chunks() -> None:
    """The explicit EOF marker belongs to the current update chunk."""
    assert parse_patch(
        _patch(
            "*** Begin Patch",
            "*** Update File: update.md",
            "@@",
            "-last",
            "+end",
            "*** End of File",
            "*** End Patch",
        )
    ) == (
        UpdateHunk(
            path="update.md",
            chunks=(
                UpdateChunk(
                    old_lines=("last",),
                    new_lines=("end",),
                    end_of_file=True,
                ),
            ),
        ),
    )


def test_patch_parses_adjacent_update_chunks() -> None:
    """An empty update chunk before another chunk is retained."""
    assert parse_patch(
        _patch(
            "*** Begin Patch",
            "*** Update File: update.md",
            "@@ first",
            "@@ second",
            "+added",
            "*** End Patch",
        )
    ) == (
        UpdateHunk(
            path="update.md",
            chunks=(
                UpdateChunk(
                    old_lines=(), new_lines=(), change_context="first"
                ),
                UpdateChunk(
                    old_lines=(),
                    new_lines=("added",),
                    change_context="second",
                ),
            ),
        ),
    )


def test_patch_parses_move_paths_for_caller_policy() -> None:
    """Move paths are parsed so mutation callers can reject them."""
    assert parse_patch(
        _patch(
            "*** Begin Patch",
            "*** Update File: old.md",
            "*** Move to: new.md",
            "@@",
            "-old",
            "+new",
            "*** End Patch",
        )
    ) == (
        UpdateHunk(
            path="old.md",
            move_path="new.md",
            chunks=(UpdateChunk(old_lines=("old",), new_lines=("new",)),),
        ),
    )


def test_patch_parses_shared_context_lines() -> None:
    """Space-prefixed update lines are retained in old and new content."""
    assert parse_patch(
        _patch(
            "*** Begin Patch",
            "*** Update File: update.md",
            "@@",
            " keep",
            "-old",
            "+new",
            "*** End Patch",
        )
    ) == (
        UpdateHunk(
            path="update.md",
            chunks=(
                UpdateChunk(
                    old_lines=("keep", "old"),
                    new_lines=("keep", "new"),
                ),
            ),
        ),
    )


def test_patch_derives_with_successful_context_anchor() -> None:
    """Context anchors can move a replacement later in the file."""
    update = derive_update(
        path="update.md",
        chunks=(
            UpdateChunk(
                old_lines=("old",),
                new_lines=("new",),
                change_context="section",
            ),
        ),
        original="old\nsection\nold\n",
    )

    assert update.content == "old\nsection\nnew\n"


def test_patch_derives_insert_only_chunks() -> None:
    """Chunks without old lines append new lines."""
    update = derive_update(
        path="update.md",
        chunks=(UpdateChunk(old_lines=(), new_lines=("added",)),),
        original="base\n",
    )

    assert update.content == "base\nadded\n"


def test_patch_derives_updates_with_trailing_empty_line_fallback() -> None:
    """Expected trailing empty lines can match files without them."""
    update = derive_update(
        path="update.md",
        chunks=(
            UpdateChunk(
                old_lines=("old", ""),
                new_lines=("new", ""),
            ),
        ),
        original="old\n",
    )

    assert update.content == "new\n"


def test_patch_keeps_nonempty_replacement_during_trailing_line_fallback() -> (
    None
):
    """Trailing-line fallback does not trim non-empty replacement lines."""
    update = derive_update(
        path="update.md",
        chunks=(
            UpdateChunk(
                old_lines=("old", ""),
                new_lines=("new",),
            ),
        ),
        original="old\n",
    )

    assert update.content == "new\n"


def test_patch_preserves_explicit_trailing_empty_replacement() -> None:
    """Replacement chunks that already end empty do not get padded again."""
    update = derive_update(
        path="update.md",
        chunks=(
            UpdateChunk(
                old_lines=("old",),
                new_lines=("new", ""),
            ),
        ),
        original="old\n",
    )

    assert update.content == "new\n"


def test_patch_derives_unicode_normalized_updates() -> None:
    """Curly punctuation and dash variants normalize during matching."""
    update = derive_update(
        path="update.md",
        chunks=(
            UpdateChunk(
                old_lines=('say "hello" - now...',),
                new_lines=("matched",),
            ),
        ),
        original="say \u201chello\u201d \u2014 now\u2026\n",
    )

    assert update.content == "matched\n"


def test_join_bom_can_omit_bom() -> None:
    """BOM joining can also emit plain text."""
    assert join_bom("\ufeffplain", bom=False) == "plain"


def test_patch_rejects_malformed_hunk_bodies() -> None:
    """Malformed patch bodies fail before any mutation uses them."""
    with pytest.raises(ValueError, match="Invalid add file line"):
        parse_patch(
            _patch(
                "*** Begin Patch",
                "*** Add File: add.md",
                "missing plus",
                "*** End Patch",
            )
        )


def test_patch_rejects_malformed_headers_and_paths() -> None:
    """Malformed patch headers and paths fail clearly."""
    bad_patches = [
        "missing markers",
        _patch("*** End Patch", "*** Begin Patch"),
        _patch(
            "*** Begin Patch",
            "*** Add File: ",
            "*** End Patch",
        ),
        _patch(
            "*** Begin Patch",
            "*** Delete File: ",
            "*** End Patch",
        ),
        _patch(
            "*** Begin Patch",
            "*** Update File: ",
            "*** End Patch",
        ),
        _patch(
            "*** Begin Patch",
            "*** Update File: old.md",
            "*** Move to: ",
            "*** End Patch",
        ),
    ]

    for patch_text in bad_patches:
        with pytest.raises(ValueError, match=r".+"):
            parse_patch(patch_text)


def test_patch_rejects_invalid_update_lines() -> None:
    """Update hunks require @@ chunks and valid chunk line prefixes."""
    with pytest.raises(ValueError, match="Invalid update file line"):
        parse_patch(
            _patch(
                "*** Begin Patch",
                "*** Update File: update.md",
                "not a chunk",
                "*** End Patch",
            )
        )

    with pytest.raises(ValueError, match="Invalid update chunk line"):
        parse_patch(
            _patch(
                "*** Begin Patch",
                "*** Update File: update.md",
                "@@",
                "not a change",
                "*** End Patch",
            )
        )


def test_patch_rejects_missing_context_and_expected_lines() -> None:
    """Derivation fails when context or expected lines are absent."""
    with pytest.raises(ValueError, match="Failed to find context"):
        derive_update(
            path="update.md",
            chunks=(
                UpdateChunk(
                    old_lines=("old",),
                    new_lines=("new",),
                    change_context="missing",
                ),
            ),
            original="old\n",
        )

    with pytest.raises(ValueError, match="Failed to find expected lines"):
        derive_update(
            path="update.md",
            chunks=(UpdateChunk(old_lines=("missing",), new_lines=("new",)),),
            original="old\n",
        )

    with pytest.raises(ValueError, match="expected at least one @@ chunk"):
        parse_patch(
            _patch(
                "*** Begin Patch",
                "*** Update File: update.md",
                "*** End Patch",
            )
        )

    with pytest.raises(ValueError, match="Invalid patch line"):
        parse_patch(
            _patch(
                "*** Begin Patch",
                "*** Delete File: delete.md",
                "unexpected body",
                "*** End Patch",
            )
        )
