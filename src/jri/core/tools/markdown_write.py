"""Scoped Markdown file writing."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from jri.core.tools.patch import (
    AddHunk,
    DeleteHunk,
    PatchHunk,
    UpdateHunk,
    derive_update,
    join_bom,
    parse_patch,
)

_locks: dict[Path, asyncio.Lock] = {}


# Public results and errors


@dataclass(frozen=True)
class MarkdownWriteResult:
    """Result of a scoped Markdown write."""

    path: Path
    bytes_written: int


@dataclass(frozen=True)
class MarkdownPatchChange:
    """One file change applied by a scoped Markdown patch."""

    operation: Literal["add", "update", "delete"]
    path: Path
    bytes_written: int


@dataclass(frozen=True)
class MarkdownPatchResult:
    """Result of a scoped Markdown patch."""

    applied: tuple[MarkdownPatchChange, ...]


class MarkdownWriteError(ValueError):
    """Raised when a Markdown write request is invalid."""


class MarkdownWriteOperations(Protocol):
    """Filesystem operations used by Markdown writes."""

    async def make_directory(self, path: Path) -> None:
        """Create a directory."""
        ...

    async def write_text(self, path: Path, content: str) -> None:
        """Write UTF-8 text."""
        ...


class MarkdownPatchOperations(MarkdownWriteOperations, Protocol):
    """Filesystem operations used by Markdown patches."""

    async def create_text(self, path: Path, content: str) -> None:
        """Create UTF-8 text without replacing an existing file."""
        ...

    async def read_bytes(self, path: Path) -> bytes:
        """Read raw file bytes."""
        ...

    async def remove_file(self, path: Path) -> None:
        """Remove a file."""
        ...


class LocalMarkdownWriteOperations:
    """Local filesystem operations for Markdown writes."""

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


async def write_markdown_file(
    *,
    allowed_root: Path,
    target_path: Path,
    content: str,
    operations: MarkdownWriteOperations | None = None,
) -> MarkdownWriteResult:
    """Write a Markdown file under an allowed root."""
    resolved_root, resolved_target = await asyncio.to_thread(
        _resolve_paths,
        allowed_root,
        target_path,
    )
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        msg = f"{resolved_target} is outside the allowed root {resolved_root}"
        raise MarkdownWriteError(msg) from exc

    writer = operations or LocalMarkdownWriteOperations()
    lock = _locks.setdefault(resolved_target, asyncio.Lock())
    async with lock:
        await writer.make_directory(resolved_target.parent)
        await writer.write_text(resolved_target, content)
    return MarkdownWriteResult(
        path=resolved_target,
        bytes_written=len(content.encode("utf-8")),
    )


async def patch_markdown_file(
    *,
    allowed_root: Path,
    target_path: Path,
    patch_text: str,
    operations: MarkdownPatchOperations | None = None,
) -> MarkdownPatchResult:
    """Apply a patch to one Markdown file under an allowed root."""
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
            raise MarkdownWriteError(msg)
    return await _apply_patch_hunks(
        resolved_root=resolved_root,
        hunks=hunks,
        operations=operations,
    )


async def patch_markdown_files(
    *,
    allowed_root: Path,
    patch_text: str,
    operations: MarkdownPatchOperations | None = None,
) -> MarkdownPatchResult:
    """Apply a structured patch under an allowed root."""
    resolved_root = await asyncio.to_thread(allowed_root.resolve)
    hunks = _parse_patch(patch_text)
    return await _apply_patch_hunks(
        resolved_root=resolved_root,
        hunks=hunks,
        operations=operations,
    )


def _resolve_paths(allowed_root: Path, target_path: Path) -> tuple[Path, Path]:
    return allowed_root.resolve(), target_path.resolve()


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
    operations: MarkdownPatchOperations | None,
) -> MarkdownPatchResult:
    writer = operations or LocalMarkdownWriteOperations()
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

    applied: list[MarkdownPatchChange] = [
        await _commit_prepared_change(writer, change) for change in prepared
    ]
    return MarkdownPatchResult(applied=tuple(applied))


async def _prepare_patch_hunks(
    *,
    writer: MarkdownPatchOperations,
    hunks: tuple[tuple[PatchHunk, Path], ...],
) -> tuple[_PreparedPatch, ...]:
    prepared: list[_PreparedPatch] = []
    for hunk, target in hunks:
        if isinstance(hunk, AddHunk):
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

        update = derive_update(
            path=hunk.path,
            chunks=hunk.chunks,
            original=source.decode("utf-8-sig"),
        )
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
    writer: MarkdownPatchOperations,
    change: _PreparedPatch,
) -> MarkdownPatchChange:
    lock = _locks.setdefault(change.target, asyncio.Lock())
    async with lock:
        if isinstance(change, _PreparedAdd):
            await writer.make_directory(change.target.parent)
            try:
                await writer.create_text(change.target, change.content)
            except FileExistsError as exc:
                msg = f"{change.hunk.path} already exists"
                raise MarkdownWriteError(msg) from exc
            return MarkdownPatchChange(
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
            raise MarkdownWriteError(msg)

        if isinstance(change, _PreparedDelete):
            await writer.remove_file(change.target)
            return MarkdownPatchChange(
                operation="delete",
                path=change.target,
                bytes_written=0,
            )

        await writer.write_text(change.target, change.content)
        return MarkdownPatchChange(
            operation="update",
            path=change.target,
            bytes_written=len(change.content.encode("utf-8")),
        )


async def _read_existing_file(
    writer: MarkdownPatchOperations,
    target: Path,
    patch_path: str,
) -> bytes:
    lock = _locks.setdefault(target, asyncio.Lock())
    async with lock:
        return await _read_existing_file_unlocked(writer, target, patch_path)


async def _read_existing_file_unlocked(
    writer: MarkdownPatchOperations,
    target: Path,
    patch_path: str,
) -> bytes:
    try:
        return await writer.read_bytes(target)
    except FileNotFoundError as exc:
        msg = f"{patch_path} does not exist"
        raise MarkdownWriteError(msg) from exc


def _parse_patch(patch_text: str) -> tuple[PatchHunk, ...]:
    try:
        hunks = parse_patch(patch_text)
    except ValueError as exc:
        raise MarkdownWriteError(str(exc)) from exc
    if not hunks:
        msg = "patch rejected: empty patch"
        raise MarkdownWriteError(msg)
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
        raise MarkdownWriteError(msg)
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
        raise MarkdownWriteError(msg) from exc


def _ensure_trailing_newline(content: str) -> str:
    if not content or content.endswith("\n"):
        return content
    return f"{content}\n"
