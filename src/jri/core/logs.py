import contextlib
import errno
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

# `jri init` writes a log record before any command configures a handler. `logging` writes a warning that
# finds no handler to the terminal, and it writes the traceback of that warning too. Add a handler that writes
# nothing. A record that JRI makes before `configure` then goes nowhere. A user must never read Python.
logging.getLogger("jri").addHandler(logging.NullHandler())

# A session outlives the process that serves it.
# `jri chat` restores its conversation, and `jri view` reads the same notes.
# All runs of a session append to one file. A session reset clears this directory.
# The file keeps the most recent records of the session. A long session loses its first records.
FILE_BYTES = 10 * 1024 * 1024
# A trim keeps this share of the file limit. New records fill the remainder before the next trim.
# A trim writes the kept records again. This share sets how many records stay, and how often JRI writes
# them again.
KEPT_SHARE = 0.5
# `open` adds these flags to the flags that its mode requires. A link can write records outside `.jri`.
# A pipe can block `open` while the run holds the log lock. Windows has neither flag, and it follows a link.
FILE_FLAGS = 0
if sys.platform != "win32":
    FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK
# These are the permissions that `open` uses to create a file.
# The umask still controls access for users other than the owner.
FILE_PERMISSIONS = 0o666
# A record reaches the file whole, or it does not reach the file at all.
# A record larger than the file limit would pass that limit before a trim.
# A fetched page, a file that JRI read, and model output can make such a record.
# Keep the first part of the record, and state how many bytes JRI removed.
# This limit must stay below `FILE_BYTES`.
RECORD_BYTES = 64 * 1024
# Stamp a record when JRI writes it, and not when JRI makes it. The runs of a session write in lock order.
# `logging` fills `%(asctime)s` before it formats the record.
# A large record could otherwise come after a later record.
# A stamp of fixed width gives the size limit of a record before the stamp exists.
STAMP = "[{time}] "
STAMP_BYTES = len(STAMP.format(time="0000-00-00 00:00:00,000"))
# `%f` includes microseconds, but the log uses milliseconds. Remove the final three stamp digits.
TIME_FORMAT = "%Y-%m-%d %H:%M:%S,%f"
TIME_MICROSECOND_DIGITS = 3
TRIM_NOTICE = "[earlier records dropped]"
TRUNCATION_NOTICE = "... [{dropped} bytes dropped]"
# This marks the source of a record that JRI cannot render.
UNRENDERED_RECORD = "unrendered_record source={source}:{line}"


def configure(settings: Settings) -> None:
    workspace = Workspace.find()
    log_file = workspace.log_file
    handler = SessionLog(log_file, workspace.log_lock_file)
    try:
        handler.repair()
        # Report an unwritable log when JRI starts. JRI would otherwise drop every later record and report none.
        log_file.touch()
    except OSError as error:
        raise PersistenceError(f"Could not create the log file `{log_file}`: {error.strerror}") from error
    # One file holds runs that overlap, and runs from different releases.
    # Every line names the release and the process, and not one banner at the top.
    # A user who copies an extract into a report can leave that banner out.
    handler.setFormatter(logging.Formatter(f"[{__version__}] [%(process)d] [%(levelname)s] [%(name)s] %(message)s"))
    application_logger = logging.getLogger("jri")
    application_logger.setLevel(settings.logging.level)
    application_logger.addHandler(handler)
    application_logger.propagate = False


class SessionLog(logging.Handler):
    def __init__(self, file: Path, lock_file: Path) -> None:
        super().__init__()
        self.file = file
        self.file_lock = Lock(lock_file)

    @override
    def emit(self, record: logging.LogRecord) -> None:
        # Render a record before JRI takes the lock.
        # JRI delays every other run when it renders a large record under the lock.
        body = self._render(record)
        if STAMP_BYTES + len(body) > RECORD_BYTES:
            # Keep room for the stamp and for the truncation notice.
            # Calculate the size of that notice from the full length of the body.
            # The body that JRI keeps cannot pass the record limit.
            room = RECORD_BYTES - STAMP_BYTES - len(TRUNCATION_NOTICE.format(dropped=len(body)))
            kept = body[:room].decode("utf-8", errors="ignore").encode()
            dropped = len(body) - len(kept)
            body = kept + TRUNCATION_NOTICE.format(dropped=dropped).encode()
        # Drop a record that JRI cannot write. `logging` reports to the terminal that a `jri chat` screen owns.
        # `jri init --force` can replace the log directory during a session.
        # The write that fails repairs the log path.
        # Do not check the path before every write. A write that works then costs only the lock, `lstat`, and open.
        try:
            self._write(body)
        except (OSError, LockError):
            with contextlib.suppress(OSError, LockError):
                self.repair()
                self._write(body)

    # Repair each log path that holds an object of the wrong type. JRI owns these paths.
    # `jri init --force` empties them, and no external software depends on them.
    # Restore the mode of a normal file, so that JRI keeps its records.
    # Remove a directory, a link, and a pipe, because they hold no log record.
    def repair(self) -> None:
        directory = self.file.parent
        if directory.is_symlink() or not directory.is_dir():
            # Remove this directory entry, and not a tree.
            # Two runs of the session can make the real directory and write it at the same time.
            with contextlib.suppress(OSError):
                directory.unlink()
        directory.mkdir(parents=True, exist_ok=True)
        _grant_owner_access(directory)
        for path in (self.file, self.file_lock.path):
            if path.is_symlink() or (path.exists() and not path.is_file()):
                _discard(path)
            else:
                _grant_owner_access(path)

    # `logging` gives a handler failure to the caller. A format failure must not end the turn that it describes.
    # Render every record, and raise nothing.
    # A record that JRI cannot render uses this formatter with a source marker.
    # Encode invalid text with Python escapes, because a repository path can hold a lone surrogate that is not UTF-8.
    # Do not handle `KeyboardInterrupt` as a failure to render.
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
        # `jri chat` and `jri view` can write this file at the same time. A trim can rewrite it under another run.
        # Stamp the record, check the size, trim the file, and append the record under one lock.
        with self.file_lock:
            stamp = datetime.now(UTC).astimezone().strftime(TIME_FORMAT)[:-TIME_MICROSECOND_DIGITS]
            line = STAMP.format(time=stamp).encode() + body + b"\n"
            try:
                # Read the size of the file that the `open` below writes, and not the target of a link at this path.
                standing = self.file.lstat()
            except FileNotFoundError:
                size = 0
            else:
                # `O_NOFOLLOW` stops the `open` below when the path is a link. Windows has no equivalent flag.
                # On Windows, the `lstat` above finds the link and raises `ELOOP`, and that error starts a repair.
                if sys.platform == "win32" and stat.S_ISLNK(standing.st_mode):
                    raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), str(self.file))
                size = standing.st_size
            if size and size + len(line) > FILE_BYTES:
                self._trim(size)
            with open(self.file, "ab", opener=_open_the_log) as stream:
                stream.write(line)

    # Keep the newest records and drop the oldest records.
    # Read and write the file under the lock that the append holds.
    # A run that wrote a record before the trim keeps that record only when it is in the part that JRI kept.
    def _trim(self, size: int) -> None:
        with open(self.file, "rb", opener=_open_the_log) as stream:
            stream.seek(max(size - int(FILE_BYTES * KEPT_SHARE), 0))
            kept = stream.read()
        # The cut is inside a record. Remove the part of that record that stayed, and report what JRI removed.
        _, _, kept = kept.partition(b"\n")
        with open(self.file, "wb", opener=_open_the_log) as stream:
            stream.write(f"{TRIM_NOTICE}\n".encode() + kept)


# Try to remove each file, and continue when a removal fails.
# An immutable file can need privileges that JRI does not have.
# A file that JRI cannot remove must not stop the repair of the next path.
def _discard(path: Path) -> None:
    with contextlib.suppress(OSError):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def _grant_owner_access(path: Path) -> None:
    with contextlib.suppress(OSError):
        mode = path.stat().st_mode
        # JRI cannot write, empty, or read a directory that gives the owner no access.
        # Restore the access of the owner only, and keep the access that other users have.
        wanted = mode | (stat.S_IRWXU if stat.S_ISDIR(mode) else stat.S_IRUSR | stat.S_IWUSR)
        if wanted != mode:
            path.chmod(wanted)


def _open_the_log(path: str, flags: int) -> int:
    return os.open(path, flags | FILE_FLAGS, FILE_PERMISSIONS)
