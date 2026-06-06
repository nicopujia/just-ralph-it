"""Tests for structured patch parsing and derivation."""

import pytest

from jri.core.tools.patch import (
    AddHunk,
    DeleteHunk,
    UpdateChunk,
    UpdateHunk,
    derive_update,
    join_bom,
    parse_patch,
)


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
