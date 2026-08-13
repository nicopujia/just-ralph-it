import logging
import shutil
from collections.abc import Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile

__all__ = ["describe_paths", "remove_directory", "shorten_path", "write_atomically"]

# Show paths that identify the read. More paths make a column, not a sentence.
MAX_DESCRIBED_PATHS = 3
NEW_FILE_PERMISSIONS = 0o644

logger = logging.getLogger(__name__)


def describe_paths(paths: Sequence[str]) -> str:
    described = [shorten_path(Path(path)) for path in paths[:MAX_DESCRIBED_PATHS]]
    if remaining := len(paths) - len(described):
        described.append(f"{remaining} more")
    last = described.pop() if described else ""
    return f"{', '.join(described)} and {last}" if described else last


# Remove a directory and all data below it. A directory that stays stops no caller that only needs it gone,
# thus report a failed removal instead of raising it.
def remove_directory(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError:
        logger.exception("directory_removal_failed path=%r", path)


def shorten_path(path: Path) -> str:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        return path.as_posix()
    # Use the process base directories. A project below a symbolic-link parent is still relative to the reader
    # directory.
    resolved = expanded.resolve()
    for base, prefix in ((Path.cwd(), ""), (Path.home(), "~/")):
        if resolved.is_relative_to(base):
            return prefix + resolved.relative_to(base).as_posix()
    return str(expanded)


def write_atomically(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    # A symbolic link identifies the file to replace, not its directory entry. Create the replacement in the target
    # file system.
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Use the JRI line ending, not the platform line ending. JRI reads this ending, and Git gets identical bytes
        # on each system.
        with NamedTemporaryFile("w", dir=target.parent, delete=False, encoding="utf-8", newline="\n") as file:
            temporary_path = Path(file.name)
            file.write(content)
        # The temporary file lets only its owner read it. Restore the replaced file permissions.
        temporary_path.chmod(target.stat().st_mode & 0o777 if target.exists() else NEW_FILE_PERMISSIONS)
        temporary_path.replace(target)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
