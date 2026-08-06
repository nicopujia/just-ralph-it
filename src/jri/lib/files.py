from collections.abc import Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile

__all__ = ["describe_paths", "shorten_path", "write_atomically"]

# Enough files to recognise the read by, before the list stops being a
# sentence and starts being a column.
MAX_DESCRIBED_PATHS = 3
NEW_FILE_PERMISSIONS = 0o644


def describe_paths(paths: Sequence[str]) -> str:
    described = [shorten_path(Path(path)) for path in paths[:MAX_DESCRIBED_PATHS]]
    if remaining := len(paths) - len(described):
        described.append(f"{remaining} more")
    last = described.pop() if described else ""
    return f"{', '.join(described)} and {last}" if described else last


def shorten_path(path: Path) -> str:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        return path.as_posix()
    # The bases are the real directories the process reports, so the
    # path is measured against them as one: a project reached through
    # a symlinked parent is still the directory the reader is in.
    resolved = expanded.resolve()
    for base, prefix in ((Path.cwd(), ""), (Path.home(), "~/")):
        if resolved.is_relative_to(base):
            return prefix + resolved.relative_to(base).as_posix()
    return str(expanded)


def write_atomically(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    # A symlink names the file whose contents to rewrite, not the entry
    # to replace, and the replacement must land on its filesystem.
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # A file JRI writes is in JRI's own format rather than the
        # platform's, so it carries the line ending it is read back
        # with and the same bytes reach Git on every machine.
        with NamedTemporaryFile("w", dir=target.parent, delete=False, encoding="utf-8", newline="\n") as file:
            temporary_path = Path(file.name)
            file.write(content)
        # The temporary file is readable by its owner alone, so the
        # permissions of the file it stands in for have to be restored.
        temporary_path.chmod(target.stat().st_mode & 0o777 if target.exists() else NEW_FILE_PERMISSIONS)
        temporary_path.replace(target)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
