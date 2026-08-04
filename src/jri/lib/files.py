from pathlib import Path
from tempfile import NamedTemporaryFile


def write_atomically(path: Path, content: str) -> None:
    """Replace a file's contents in one step, making its directory.

    Readers see either the previous contents or the new ones, so a
    process killed mid-write leaves nothing half-written behind.

    Raises:
        OSError: If the directory, the temporary file, or the
            replacement cannot be written.
    """

    temporary_path: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as file:
            temporary_path = file.name
            file.write(content)
        Path(temporary_path).replace(path)
    except OSError:
        if temporary_path is not None:
            Path(temporary_path).unlink(missing_ok=True)
        raise
