import contextlib
import errno
import itertools
import logging
import os
import shutil
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import override

from jri import __version__
from jri.lib.lock import Lock, LockError

from .exceptions import PersistenceError
from .settings import Settings
from .workspace import Workspace

# A session outlives its serving process. `jri chat` restores its conversation, and `jri view` reads the same notes.
# All session runs append here. A session reset clears this directory.
# Preserve the opening log permanently. A report zips this directory and must include setup, not only the latest window.
# This directory cannot preserve log data that a window drops.
KEPT_LOG_FILES = 3
LOG_FILE_BYTES = 5 * 1024 * 1024
# `open` adds these flags to flags required by its mode. A link can write records outside `.jri`.
# A pipe can block the open while the run holds the log lock. Windows has neither flag and follows a link.
LOG_FILE_FLAGS = 0
if sys.platform != "win32":
    LOG_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK
# These are the permissions that `open` uses to create a file.
# The umask still controls access for users other than the owner.
LOG_FILE_PERMISSIONS = 0o666
# A record reaches the file whole or not at all. A record over the file limit would exceed that limit before rotation.
# Fetched pages, read files, and model output can create such records.
# Keep their beginning and state the removed byte count.
# This limit must remain below `LOG_FILE_BYTES`.
LOG_RECORD_BYTES = 64 * 1024
# Stamp a record when it is written, not when it is created. Session runs write in lock order.
# `%(asctime)s` is created before formatting. A large record can otherwise appear after a later record.
# A fixed stamp width gives the record size limit before the stamp exists.
LOG_STAMP = "[{time}] "
LOG_STAMP_BYTES = len(LOG_STAMP.format(time="0000-00-00 00:00:00,000"))
# `%f` includes microseconds, but the log uses milliseconds. Remove the final three stamp digits.
LOG_TIME_FORMAT = "%Y-%m-%d %H:%M:%S,%f"
LOG_TIME_MICROSECOND_DIGITS = 3
TRUNCATION_NOTICE = "... [{dropped} bytes dropped]"
# This marks the source of a record that cannot be rendered.
UNRENDERED_RECORD = "unrendered_record source={source}:{line}"


def configure(settings: Settings) -> None:
    workspace = Workspace.find()
    log_file = workspace.log_file
    handler = SessionLog(log_file, workspace.log_lock_file)
    try:
        handler.repair()
        # Report an unwritable log at startup. Every later record would be dropped instead of reported.
        log_file.touch()
    except OSError as error:
        raise PersistenceError(f"Could not create the log file `{log_file}`: {error.strerror}") from error
    # One file holds overlapping runs and runs from different releases. Each line carries both identifiers.
    # A log-level banner could be omitted from the report file.
    handler.setFormatter(logging.Formatter(f"[{__version__}] [%(process)d] [%(levelname)s] [%(name)s] %(message)s"))
    application_logger = logging.getLogger("jri")
    application_logger.setLevel(settings.logging.level)
    application_logger.addHandler(handler)
    application_logger.propagate = False


class SessionLog(logging.Handler):
    def __init__(self, file: Path, lock_file: Path) -> None:
        super().__init__()
        self.file = file
        # Store files from oldest to newest: the opening, rotated files, and the current file.
        self.kept_files = tuple(
            file.with_name(f"{file.name}.{index}") if index else file for index in reversed(range(KEPT_LOG_FILES))
        )
        self.file_lock = Lock(lock_file)

    @override
    def emit(self, record: logging.LogRecord) -> None:
        # Render a record before acquiring the lock. Rendering a large record while locked delays every other run.
        body = self._render(record)
        if LOG_STAMP_BYTES + len(body) > LOG_RECORD_BYTES:
            # Reserve space for the stamp and truncation notice. Calculate notice size from the full body length.
            # The kept body cannot exceed the record limit.
            room = LOG_RECORD_BYTES - LOG_STAMP_BYTES - len(TRUNCATION_NOTICE.format(dropped=len(body)))
            kept = body[:room].decode("utf-8", errors="ignore").encode()
            dropped = len(body) - len(kept)
            body = kept + TRUNCATION_NOTICE.format(dropped=dropped).encode()
        # Drop a record that cannot be written. Logging reports to the terminal that a `jri chat` screen owns.
        # `jri init --force` can replace the log directory during a session. The failing write repairs the log path.
        # Do not check the path before every write. Correct writes pay only for the lock, `lstat`, and open.
        try:
            self._write(body)
        except (OSError, LockError):
            with contextlib.suppress(OSError, LockError):
                self.repair()
                self._write(body)

    # Repair any object at a required log path that is not the required object. These paths are owned by JRI.
    # `jri init --force` empties them, and external software does not depend on them.
    # Restore the mode of a normal file to preserve its records.
    # Remove directories, links, and pipes because they hold no log records.
    def repair(self) -> None:
        directory = self.file.parent
        if directory.is_symlink() or not directory.is_dir():
            # Remove this directory entry, not a tree. Concurrent session runs can create and write the real directory.
            with contextlib.suppress(OSError):
                directory.unlink()
        directory.mkdir(parents=True, exist_ok=True)
        _grant_owner_access(directory)
        for path in (*self.kept_files, self.file_lock.path):
            if path.is_symlink() or (path.exists() and not path.is_file()):
                _discard(path)
            else:
                _grant_owner_access(path)

    # `logging` gives handler failures to the caller. A formatting failure must not end the turn that it describes.
    # Render every record without raising. An unrenderable record uses this formatter with a source marker.
    # Encode invalid text with Python escapes. A repository path can contain a non-UTF-8 lone surrogate.
    # Do not handle `KeyboardInterrupt` as a rendering failure.
    def _render(self, record: logging.LogRecord) -> bytes:
        rendered: str | None = None
        with contextlib.suppress(Exception):
            rendered = self.format(record)
        if rendered is None:
            marker = UNRENDERED_RECORD.format(source=record.filename, line=record.lineno)
            fallen = logging.LogRecord(record.name, record.levelno, record.pathname, record.lineno, marker, None, None)
            rendered = self.format(fallen)
        return rendered.encode(errors="backslashreplace")

    def _write(self, body: bytes) -> None:
        # `jri chat` and `jri view` can write this file at the same time. Rotation can rename it under another run.
        # Stamp, size check, rotation, and append must occur under one lock.
        with self.file_lock:
            stamp = datetime.now(UTC).astimezone().strftime(LOG_TIME_FORMAT)[:-LOG_TIME_MICROSECOND_DIGITS]
            line = LOG_STAMP.format(time=stamp).encode() + body + b"\n"
            try:
                # Read the size of the file that the open below writes, not the target of a link at this path.
                standing = self.file.lstat()
            except FileNotFoundError:
                size = 0
            else:
                # `O_NOFOLLOW` prevents the open below from following a link. Windows has no equivalent flag.
                # On Windows, the existing `lstat` detects the link and raises `ELOOP`, which triggers repair.
                if sys.platform == "win32" and stat.S_ISLNK(standing.st_mode):
                    raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), str(self.file))
                size = standing.st_size
            if size and size + len(line) > LOG_FILE_BYTES:
                self._rotate()
            with open(self.file, "ab", opener=_open_the_log) as stream:
                stream.write(line)

    def _rotate(self) -> None:
        opening, *window = self.kept_files
        # The first rotation would drop the session opening, so freeze that file.
        # Later rotations move only the rotating window.
        # A path that does not hold a log file holds no records. Let its rename fail and trigger repair.
        if not opening.is_file():
            self.file.replace(opening)
            return
        for older, newer in itertools.pairwise(window):
            if newer.is_file():
                newer.replace(older)


# Remove files on a best-effort basis. An immutable file can require unavailable privileges.
# One failed removal must not prevent repair of the next path.
def _discard(path: Path) -> None:
    with contextlib.suppress(OSError):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def _grant_owner_access(path: Path) -> None:
    with contextlib.suppress(OSError):
        mode = path.stat().st_mode
        # A directory without owner access cannot be written, emptied, or read for rotated files.
        # Restore only owner access. Preserve access configured for other users.
        wanted = mode | (stat.S_IRWXU if stat.S_ISDIR(mode) else stat.S_IRUSR | stat.S_IWUSR)
        if wanted != mode:
            path.chmod(wanted)


def _open_the_log(path: str, flags: int) -> int:
    return os.open(path, flags | LOG_FILE_FLAGS, LOG_FILE_PERMISSIONS)
