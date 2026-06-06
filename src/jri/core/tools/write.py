"""Scoped file writing and structured patching."""

import asyncio
import operator
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

_locks: dict[Path, asyncio.Lock] = {}


# Public write results and errors


@dataclass(frozen=True)
class WriteResult:
    """Result of a scoped file write."""

    path: Path
    bytes_written: int


@dataclass(frozen=True)
class PatchChange:
    """One file change applied by a scoped patch."""

    operation: Literal["add", "update", "delete"]
    path: Path
    bytes_written: int


@dataclass(frozen=True)
class PatchResult:
    """Result of a scoped patch."""

    applied: tuple[PatchChange, ...]


class WriteError(ValueError):
    """Raised when a scoped write request is invalid."""


class WriteOperations(Protocol):
    """Filesystem operations used by scoped writes."""

    async def make_directory(self, path: Path) -> None:
        """Create a directory."""
        ...

    async def write_text(self, path: Path, content: str) -> None:
        """Write UTF-8 text."""
        ...


class PatchOperations(WriteOperations, Protocol):
    """Filesystem operations used by scoped patches."""

    async def create_text(self, path: Path, content: str) -> None:
        """Create UTF-8 text without replacing an existing file."""
        ...

    async def read_bytes(self, path: Path) -> bytes:
        """Read raw file bytes."""
        ...

    async def remove_file(self, path: Path) -> None:
        """Remove a file."""
        ...


class LocalWriteOperations:
    """Local filesystem operations for scoped writes."""

    @staticmethod
    async def make_directory(path: Path) -> None:
        """Create a local directory."""
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)

    @staticmethod
    async def write_text(path: Path, content: str) -> None:
        """Write local UTF-8 text."""
        await asyncio.to_thread(path.write_text, content, encoding="utf-8")

    @staticmethod
    async def create_text(path: Path, content: str) -> None:
        """Create local text without replacing an existing file."""

        def create() -> None:
            with path.open("x", encoding="utf-8") as target:
                target.write(content)

        await asyncio.to_thread(create)

    @staticmethod
    async def read_bytes(path: Path) -> bytes:
        """Read local bytes."""
        return await asyncio.to_thread(path.read_bytes)

    @staticmethod
    async def remove_file(path: Path) -> None:
        """Remove a local file."""
        await asyncio.to_thread(path.unlink)


# Public patch data structures


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


type PatchHunk = AddHunk | DeleteHunk | UpdateHunk


@dataclass(frozen=True)
class FileUpdate:
    """Updated text plus whether to preserve a UTF-8 BOM."""

    content: str
    bom: bool


# Public write functions


async def write_file(
    *,
    allowed_root: Path,
    target_path: Path,
    content: str,
    operations: WriteOperations | None = None,
) -> WriteResult:
    """Write a file under an allowed root."""
    resolved_root, resolved_target = await asyncio.to_thread(
        _resolve_paths,
        allowed_root,
        target_path,
    )
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        msg = f"{resolved_target} is outside the allowed root {resolved_root}"
        raise WriteError(msg) from exc

    writer = operations or LocalWriteOperations()
    lock = _locks.setdefault(resolved_target, asyncio.Lock())
    async with lock:
        await writer.make_directory(resolved_target.parent)
        await writer.write_text(resolved_target, content)
    return WriteResult(
        path=resolved_target,
        bytes_written=len(content.encode("utf-8")),
    )


async def patch_file(
    *,
    allowed_root: Path,
    target_path: Path,
    patch_text: str,
    operations: PatchOperations | None = None,
) -> PatchResult:
    """Apply a patch to one file under an allowed root."""
    resolved_root, resolved_target = await asyncio.to_thread(
        _resolve_paths,
        allowed_root,
        target_path,
    )
    _validate_target_within_root(
        resolved_root=resolved_root,
        resolved_target=resolved_target,
    )
    hunks = _parse_patch(patch_text)
    for hunk in hunks:
        hunk_target = _resolve_hunk_target(resolved_root, hunk.path)
        if hunk_target != resolved_target:
            msg = (
                f"Patch hunk {hunk.path} does not target "
                f"{resolved_target.relative_to(resolved_root).as_posix()}"
            )
            raise WriteError(msg)
    return await _apply_patch_hunks(
        resolved_root=resolved_root,
        hunks=hunks,
        operations=operations,
    )


async def patch_files(
    *,
    allowed_root: Path,
    patch_text: str,
    operations: PatchOperations | None = None,
) -> PatchResult:
    """Apply a structured patch under an allowed root."""
    resolved_root = await asyncio.to_thread(allowed_root.resolve)
    hunks = _parse_patch(patch_text)
    return await _apply_patch_hunks(
        resolved_root=resolved_root,
        hunks=hunks,
        operations=operations,
    )


# Public patch parsing and derivation functions


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


# Patch preparation


@dataclass(frozen=True)
class _PreparedAdd:
    hunk: AddHunk
    target: Path
    content: str


@dataclass(frozen=True)
class _PreparedDelete:
    hunk: DeleteHunk
    target: Path
    source: bytes


@dataclass(frozen=True)
class _PreparedUpdate:
    hunk: UpdateHunk
    target: Path
    source: bytes
    content: str


type _PreparedPatch = _PreparedAdd | _PreparedDelete | _PreparedUpdate


async def _apply_patch_hunks(
    *,
    resolved_root: Path,
    hunks: tuple[PatchHunk, ...],
    operations: PatchOperations | None,
) -> PatchResult:
    writer = operations or LocalWriteOperations()
    targets = [
        _resolve_and_validate_hunk(
            resolved_root=resolved_root,
            hunk=hunk,
        )
        for hunk in hunks
    ]
    prepared = await _prepare_patch_hunks(
        writer=writer,
        hunks=tuple(zip(hunks, targets, strict=True)),
    )

    applied: list[PatchChange] = [
        await _commit_prepared_change(writer, change) for change in prepared
    ]
    return PatchResult(applied=tuple(applied))


async def _prepare_patch_hunks(
    *,
    writer: PatchOperations,
    hunks: tuple[tuple[PatchHunk, Path], ...],
) -> tuple[_PreparedPatch, ...]:
    prepared: list[_PreparedPatch] = []
    for hunk, target in hunks:
        if isinstance(hunk, AddHunk):
            await _validate_missing_file(writer, target, hunk.path)
            prepared.append(
                _PreparedAdd(
                    hunk=hunk,
                    target=target,
                    content=_ensure_trailing_newline(hunk.contents),
                )
            )
            continue

        source = await _read_existing_file(writer, target, hunk.path)
        if isinstance(hunk, DeleteHunk):
            prepared.append(
                _PreparedDelete(hunk=hunk, target=target, source=source)
            )
            continue

        try:
            update = derive_update(
                path=hunk.path,
                chunks=hunk.chunks,
                original=source.decode("utf-8-sig"),
            )
        except ValueError as exc:
            raise WriteError(str(exc)) from exc
        prepared.append(
            _PreparedUpdate(
                hunk=hunk,
                target=target,
                source=source,
                content=join_bom(update.content, bom=update.bom),
            )
        )
    return tuple(prepared)


# Patch commit and validation helpers


async def _commit_prepared_change(
    writer: PatchOperations,
    change: _PreparedPatch,
) -> PatchChange:
    lock = _locks.setdefault(change.target, asyncio.Lock())
    async with lock:
        if isinstance(change, _PreparedAdd):
            await writer.make_directory(change.target.parent)
            try:
                await writer.create_text(change.target, change.content)
            except FileExistsError as exc:
                msg = f"{change.hunk.path} already exists"
                raise WriteError(msg) from exc
            return PatchChange(
                operation="add",
                path=change.target,
                bytes_written=len(change.content.encode("utf-8")),
            )

        current = await _read_existing_file_unlocked(
            writer,
            change.target,
            change.hunk.path,
        )
        if current != change.source:
            msg = f"{change.hunk.path} changed before patch could be applied"
            raise WriteError(msg)

        if isinstance(change, _PreparedDelete):
            await writer.remove_file(change.target)
            return PatchChange(
                operation="delete",
                path=change.target,
                bytes_written=0,
            )

        await writer.write_text(change.target, change.content)
        return PatchChange(
            operation="update",
            path=change.target,
            bytes_written=len(change.content.encode("utf-8")),
        )


async def _read_existing_file(
    writer: PatchOperations,
    target: Path,
    patch_path: str,
) -> bytes:
    lock = _locks.setdefault(target, asyncio.Lock())
    async with lock:
        return await _read_existing_file_unlocked(writer, target, patch_path)


async def _validate_missing_file(
    writer: PatchOperations,
    target: Path,
    patch_path: str,
) -> None:
    try:
        await writer.read_bytes(target)
    except FileNotFoundError:
        return
    msg = f"{patch_path} already exists"
    raise WriteError(msg)


async def _read_existing_file_unlocked(
    writer: PatchOperations,
    target: Path,
    patch_path: str,
) -> bytes:
    try:
        return await writer.read_bytes(target)
    except FileNotFoundError as exc:
        msg = f"{patch_path} does not exist"
        raise WriteError(msg) from exc


def _parse_patch(patch_text: str) -> tuple[PatchHunk, ...]:
    try:
        hunks = parse_patch(patch_text)
    except ValueError as exc:
        raise WriteError(str(exc)) from exc
    if not hunks:
        msg = "patch rejected: empty patch"
        raise WriteError(msg)
    move = next(
        (
            hunk
            for hunk in hunks
            if isinstance(hunk, UpdateHunk) and hunk.move_path is not None
        ),
        None,
    )
    if move is not None:
        msg = "patch moves are not supported yet"
        raise WriteError(msg)
    return hunks


def _resolve_and_validate_hunk(
    *,
    resolved_root: Path,
    hunk: PatchHunk,
) -> Path:
    target = _resolve_hunk_target(resolved_root, hunk.path)
    _validate_target_within_root(
        resolved_root=resolved_root,
        resolved_target=target,
    )
    return target


def _resolve_hunk_target(resolved_root: Path, path: str) -> Path:
    requested = Path(path)
    target = (
        requested if requested.is_absolute() else resolved_root / requested
    )
    return target.resolve()


def _validate_target_within_root(
    *,
    resolved_root: Path,
    resolved_target: Path,
) -> None:
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        msg = f"{resolved_target} is outside the allowed root {resolved_root}"
        raise WriteError(msg) from exc


def _ensure_trailing_newline(content: str) -> str:
    if not content or content.endswith("\n"):
        return content
    return f"{content}\n"


def _resolve_paths(allowed_root: Path, target_path: Path) -> tuple[Path, Path]:
    return allowed_root.resolve(), target_path.resolve()


# Patch parsing helpers


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
