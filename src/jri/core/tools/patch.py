"""Structured patch parsing and derivation."""

import operator
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

type PatchHunk = AddHunk | DeleteHunk | UpdateHunk


# Public data structures


@dataclass(frozen=True)
class AddHunk:
    """Patch hunk that creates a file."""

    path: str
    contents: str
    type: Literal["add"] = "add"


@dataclass(frozen=True)
class DeleteHunk:
    """Patch hunk that deletes a file."""

    path: str
    type: Literal["delete"] = "delete"


@dataclass(frozen=True)
class UpdateChunk:
    """One replace/insert chunk in an update hunk."""

    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    change_context: str | None = None
    end_of_file: bool = False


@dataclass(frozen=True)
class UpdateHunk:
    """Patch hunk that updates a file."""

    path: str
    chunks: tuple[UpdateChunk, ...]
    move_path: str | None = None
    type: Literal["update"] = "update"


@dataclass(frozen=True)
class FileUpdate:
    """Updated text plus whether to preserve a UTF-8 BOM."""

    content: str
    bom: bool


def parse_patch(patch_text: str) -> tuple[PatchHunk, ...]:
    """Parse structured patch text into hunks."""
    lines = _strip_heredoc(patch_text.strip()).split("\n")
    begin, end = _find_patch_bounds(lines)
    hunks: list[PatchHunk] = []
    index = begin + 1
    while index < end:
        hunk, index = _parse_hunk(lines, index)
        hunks.append(hunk)
    return tuple(hunks)


def derive_update(
    *,
    path: str,
    chunks: Sequence[UpdateChunk],
    original: str,
) -> FileUpdate:
    """Derive updated file text by applying update chunks."""
    source = split_bom(original)
    lines = source.content.split("\n")
    if lines and not lines[-1]:
        lines.pop()
    replacements = _compute_replacements(lines, path, chunks)
    updated = list(lines)
    for start, remove, insert in reversed(replacements):
        updated[start : start + remove] = insert
    if not updated or updated[-1]:
        updated.append("")
    next_update = split_bom("\n".join(updated))
    return FileUpdate(
        content=next_update.content,
        bom=source.bom or next_update.bom,
    )


@dataclass(frozen=True)
class _SplitBom:
    content: str
    bom: bool


def split_bom(text: str) -> _SplitBom:
    """Split one leading UTF-8 BOM marker from text."""
    if text.startswith("\ufeff"):
        return _SplitBom(content=text[1:], bom=True)
    return _SplitBom(content=text, bom=False)


def join_bom(text: str, *, bom: bool) -> str:
    """Join text with at most one leading UTF-8 BOM marker."""
    stripped = split_bom(text).content
    return f"\ufeff{stripped}" if bom else stripped


# Parsing helpers


def _find_patch_bounds(lines: Sequence[str]) -> tuple[int, int]:
    try:
        begin = next(
            index
            for index, line in enumerate(lines)
            if line.strip() == "*** Begin Patch"
        )
        end = next(
            index
            for index, line in enumerate(lines)
            if line.strip() == "*** End Patch"
        )
    except StopIteration as exc:
        msg = "Invalid patch format: missing Begin/End markers"
        raise ValueError(msg) from exc
    if begin >= end:
        msg = "Invalid patch format: missing Begin/End markers"
        raise ValueError(msg)
    return begin, end


def _parse_hunk(
    lines: Sequence[str],
    index: int,
) -> tuple[PatchHunk, int]:
    line = lines[index]
    if line.startswith("*** Add File:"):
        return _parse_add_hunk(lines, index)
    if line.startswith("*** Delete File:"):
        return _parse_delete_hunk(line), index + 1
    if line.startswith("*** Update File:"):
        return _parse_update_hunk(lines, index)
    msg = f"Invalid patch line: {line}"
    raise ValueError(msg)


def _parse_add_hunk(
    lines: Sequence[str],
    index: int,
) -> tuple[AddHunk, int]:
    path = lines[index].removeprefix("*** Add File:").strip()
    if not path:
        msg = "Invalid add file path"
        raise ValueError(msg)
    content, next_index = _parse_add(lines, index + 1)
    return AddHunk(path=path, contents=content), next_index


def _parse_delete_hunk(line: str) -> DeleteHunk:
    path = line.removeprefix("*** Delete File:").strip()
    if not path:
        msg = "Invalid delete file path"
        raise ValueError(msg)
    return DeleteHunk(path=path)


def _parse_update_hunk(
    lines: Sequence[str],
    index: int,
) -> tuple[UpdateHunk, int]:
    path = lines[index].removeprefix("*** Update File:").strip()
    if not path:
        msg = "Invalid update file path"
        raise ValueError(msg)
    index += 1
    move_path = None
    if lines[index].startswith("*** Move to:"):
        move_path = lines[index].removeprefix("*** Move to:").strip()
        if not move_path:
            msg = "Invalid move file path"
            raise ValueError(msg)
        index += 1
    chunks, next_index = _parse_update(lines, index)
    if not chunks:
        msg = f"Invalid update hunk for {path}: expected at least one @@ chunk"
        raise ValueError(msg)
    return (
        UpdateHunk(
            path=path,
            move_path=move_path,
            chunks=tuple(chunks),
        ),
        next_index,
    )


def _parse_add(
    lines: Sequence[str],
    start: int,
) -> tuple[str, int]:
    content: list[str] = []
    index = start
    while index < len(lines) and not lines[index].startswith("***"):
        if not lines[index].startswith("+"):
            msg = f"Invalid add file line: {lines[index]}"
            raise ValueError(msg)
        content.append(lines[index][1:])
        index += 1
    return "\n".join(content), index


def _parse_update(
    lines: Sequence[str],
    start: int,
) -> tuple[list[UpdateChunk], int]:
    chunks: list[UpdateChunk] = []
    index = start
    while index < len(lines) and not lines[index].startswith("***"):
        if not lines[index].startswith("@@"):
            msg = f"Invalid update file line: {lines[index]}"
            raise ValueError(msg)
        change_context = lines[index][2:].strip() or None
        old_lines: list[str] = []
        new_lines: list[str] = []
        end_of_file = False
        index += 1
        while index < len(lines) and not lines[index].startswith("@@"):
            line = lines[index]
            if line == "*** End of File":
                end_of_file = True
                index += 1
                break
            if line.startswith("***"):
                break
            if line.startswith(" "):
                old_lines.append(line[1:])
                new_lines.append(line[1:])
            elif line.startswith("-"):
                old_lines.append(line[1:])
            elif line.startswith("+"):
                new_lines.append(line[1:])
            else:
                msg = f"Invalid update chunk line: {line}"
                raise ValueError(msg)
            index += 1
        chunks.append(
            UpdateChunk(
                old_lines=tuple(old_lines),
                new_lines=tuple(new_lines),
                change_context=change_context,
                end_of_file=end_of_file,
            )
        )
    return chunks, index


# Update derivation helpers


def _compute_replacements(
    lines: Sequence[str],
    path: str,
    chunks: Sequence[UpdateChunk],
) -> list[tuple[int, int, tuple[str, ...]]]:
    replacements: list[tuple[int, int, tuple[str, ...]]] = []
    line_index = 0
    for chunk in chunks:
        if chunk.change_context:
            context = _seek(lines, (chunk.change_context,), line_index)
            if context == -1:
                msg = (
                    f"Failed to find context {chunk.change_context!r} "
                    f"in {path}"
                )
                raise ValueError(msg)
            line_index = context + 1
        if not chunk.old_lines:
            replacements.append((len(lines), 0, chunk.new_lines))
            continue
        old_lines = chunk.old_lines
        new_lines = chunk.new_lines
        found = _seek(
            lines,
            old_lines,
            line_index,
            eof=chunk.end_of_file,
        )
        if found == -1 and not old_lines[-1]:
            old_lines = old_lines[:-1]
            if new_lines and not new_lines[-1]:
                new_lines = new_lines[:-1]
            found = _seek(
                lines,
                old_lines,
                line_index,
                eof=chunk.end_of_file,
            )
        if found == -1:
            msg = f"Failed to find expected lines in {path}:\n" + "\n".join(
                chunk.old_lines
            )
            raise ValueError(msg)
        replacements.append((found, len(old_lines), new_lines))
        line_index = found + len(old_lines)
    return sorted(replacements, key=operator.itemgetter(0))


def _seek(
    lines: Sequence[str],
    pattern: Sequence[str],
    start: int,
    *,
    eof: bool = False,
) -> int:
    for compare in (operator.eq, _rstrip, _trim, _normalized):
        if eof:
            offset = len(lines) - len(pattern)
            if offset >= start and _matches(lines, pattern, offset, compare):
                return offset
            continue
        for offset in range(start, len(lines) - len(pattern) + 1):
            if _matches(lines, pattern, offset, compare):
                return offset
    return -1


def _matches(
    lines: Sequence[str],
    pattern: Sequence[str],
    offset: int,
    compare: Callable[[str, str], bool],
) -> bool:
    return all(
        compare(lines[offset + index], line)
        for index, line in enumerate(pattern)
    )


def _rstrip(left: str, right: str) -> bool:
    return left.rstrip() == right.rstrip()


def _trim(left: str, right: str) -> bool:
    return left.strip() == right.strip()


def _normalized(left: str, right: str) -> bool:
    return _normalize(left.strip()) == _normalize(right.strip())


_NORMALIZATION_TRANSLATION = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2026": "...",
    "\u00a0": " ",
})


def _normalize(value: str) -> str:
    return value.translate(_NORMALIZATION_TRANSLATION)


def _strip_heredoc(input_text: str) -> str:
    match = re.match(
        r"^(?:cat\s+)?<<['\"]?(\w+)['\"]?\s*\n([\s\S]*?)\n\1\s*$",
        input_text,
    )
    if match is None:
        return input_text
    return match.group(2)
