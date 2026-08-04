from pathlib import Path
from tempfile import NamedTemporaryFile

__all__ = ["write_atomically"]

NEW_FILE_PERMISSIONS = 0o644


def write_atomically(path: Path, content: str) -> None:
    # Readers see either the previous contents or the new ones, so a
    # process killed mid-write leaves nothing half-written behind.
    temporary_path: Path | None = None
    # A symlink names the file whose contents to rewrite, not the entry
    # to replace, and the replacement must land on its filesystem.
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with NamedTemporaryFile("w", dir=target.parent, delete=False, encoding="utf-8") as file:
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
