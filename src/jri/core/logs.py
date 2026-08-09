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

# A session outlives the process serving it: `jri chat` reads the same
# conversation back out of the workspace every time it is started, and a
# `jri view` beside it reports on the same notes. So the runs of one
# session append to one file, and what ends a session -- the reset that
# drops the conversation -- is what clears the directory holding it.
# The oldest of these files is the session's opening, written once and
# never rotated again: a report is made by zipping this directory up,
# and a session that fills the window over and over would otherwise
# hand over its last few minutes and nothing of how it was set up. What
# the directory cannot promise is the middle of such a session, which
# the window drops and nothing here puts back.
KEPT_LOG_FILES = 3
LOG_FILE_BYTES = 5 * 1024 * 1024
# `open` works out the flags the mode it was handed needs and these go
# on top of them. A link on the log's name would put the records
# wherever it points, which is how a run writes outside `.jri`; a pipe
# on that name blocks the open until somebody reads, which hangs the
# run with the log's lock in its hand. Windows has neither flag, and no
# pipe of its own answers to a path, so a link there is followed.
LOG_FILE_FLAGS = 0
if sys.platform != "win32":
    LOG_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK
# What `open` would have created the file with, so the umask still
# decides who besides the owner may read a run's records.
LOG_FILE_PERMISSIONS = 0o666
# A record reaches the file whole or not at all, so one longer than the
# file bound would leave a file past that bound and take every record
# before it down on the rotation the next one makes. What grows this
# far is a page fetched, a file read or a model's output logged at
# DEBUG, and it is the front of such a record that names what happened,
# so the rest goes and what went is counted on the line. This must stay
# under `LOG_FILE_BYTES` for the bound over the files to hold.
LOG_RECORD_BYTES = 64 * 1024
# A record is stamped where it is written rather than where it is made.
# The runs of one session take turns over the file, so the order it
# reads back in is the order the lock hands out, and `%(asctime)s` is
# stamped at creation: a run that spends a millisecond formatting a
# large record lands it behind records its neighbour created later, and
# the truncation cutting such a record down is time spent inside that
# window. The width is fixed, so what a record has room for is known
# before the stamp exists.
LOG_STAMP = "[{time}] "
LOG_STAMP_BYTES = len(LOG_STAMP.format(time="0000-00-00 00:00:00,000"))
# `%f` carries microseconds and the log is stamped to the millisecond,
# so the last three digits of a stamp go.
LOG_TIME_FORMAT = "%Y-%m-%d %H:%M:%S,%f"
LOG_TIME_MICROSECOND_DIGITS = 3
TRUNCATION_NOTICE = "... [{dropped} bytes dropped]"
# Where the call that made the record is, since what the record was
# going to say is the thing that could not be said.
UNRENDERED_RECORD = "unrendered_record source={source}:{line}"


def configure(settings: Settings) -> None:
    workspace = Workspace.find()
    log_file = workspace.log_file
    handler = SessionLog(log_file, workspace.log_lock_file)
    try:
        handler.repair()
        # A log nothing may write to is worth saying at the start,
        # since every record after this is dropped rather than reported.
        log_file.touch()
    except OSError as error:
        raise PersistenceError(f"Could not create the log file `{log_file}`: {error.strerror}") from error
    # One file now holds runs that overlap and runs made by different
    # releases, which a file per run used to tell apart by existing, so
    # the line carries both rather than a banner a configured level
    # could keep out of the file the report is made from.
    handler.setFormatter(logging.Formatter(f"[{__version__}] [%(process)d] [%(levelname)s] [%(name)s] %(message)s"))
    application_logger = logging.getLogger("jri")
    application_logger.setLevel(settings.logging.level)
    application_logger.addHandler(handler)
    application_logger.propagate = False


class SessionLog(logging.Handler):
    def __init__(self, file: Path, lock_file: Path) -> None:
        super().__init__()
        self.file = file
        # Oldest first: the opening, then the window rotation walks in
        # order, then the file being written to now.
        self.kept_files = tuple(
            file.with_name(f"{file.name}.{index}") if index else file for index in reversed(range(KEPT_LOG_FILES))
        )
        self.file_lock = Lock(lock_file)

    @override
    def emit(self, record: logging.LogRecord) -> None:
        # Rendering a record costs what the record is long, so it is
        # done out here: a run holding the lock for the milliseconds a
        # large record takes is a run every other one waits behind.
        body = self._render(record)
        if LOG_STAMP_BYTES + len(body) > LOG_RECORD_BYTES:
            # The stamp and the notice take their room out of the
            # bound, and the widest count the notice can carry is the
            # length it is measured against, so what is left is never
            # past the bound.
            room = LOG_RECORD_BYTES - LOG_STAMP_BYTES - len(TRUNCATION_NOTICE.format(dropped=len(body)))
            kept = body[:room].decode("utf-8", errors="ignore").encode()
            dropped = len(body) - len(kept)
            body = kept + TRUNCATION_NOTICE.format(dropped=dropped).encode()
        # A record that cannot be written is dropped rather than
        # reported, since the stream `logging` reports on is the
        # terminal a `jri chat` screen holds. So nothing is left to
        # notice a path of the log's that has stopped being what it
        # must be -- a `jri init --force` beside a running session
        # takes the directory, and a run whose records all land inside
        # it is silent from there until somebody else happens to put it
        # right. The write that finds a path wrong is the write that
        # repairs it, so the records that find them right pay a lock, a
        # `lstat` and an open, and nothing for the look.
        try:
            self._write(body)
        except (OSError, LockError):
            with contextlib.suppress(OSError, LockError):
                self.repair()
                self._write(body)

    # Whatever stands on a path the log needs and is not what the log
    # needs there is put right rather than worked around: this
    # directory and the files under it are JRI's own, `jri init
    # --force` empties them, and nothing outside the run is going to
    # notice. A mode is set back, since the records the file already
    # holds are worth more than a fresh one; anything else -- a
    # directory, a link, a pipe -- is removed, since none of them holds
    # a record of the log's to lose.
    def repair(self) -> None:
        directory = self.file.parent
        if directory.is_symlink() or not directory.is_dir():
            # The name and never a tree: the runs of a session repair
            # beside each other, and one that finds no directory here
            # must not empty the real one another has made in the
            # meantime and is writing into now.
            with contextlib.suppress(OSError):
                directory.unlink()
        directory.mkdir(parents=True, exist_ok=True)
        _grant_owner_access(directory)
        for path in (*self.kept_files, self.file_lock.path):
            if path.is_symlink() or (path.exists() and not path.is_file()):
                _discard(path)
            else:
                _grant_owner_access(path)

    # `logging` hands whatever a handler raises to whoever logged, and
    # JRI logs from inside the generator a turn is, which `_run_turn`
    # consumes under an `except Exception`: a record whose arguments do
    # not fit its format, or holding an object whose `__str__` raises,
    # would end the turn it was describing. So rendering is total, for
    # every way a record can refuse rather than the ways anybody has
    # met. What will not render at all leaves a marker where it fell,
    # through this same formatter, so the line still names the release,
    # the process, the level and the logger the way every other line
    # does. What renders but will not encode is kept in the escapes
    # Python writes it in rather than lost: `jri.lib.git` decodes every
    # path a repository holds with `os.fsdecode`, so a name of the
    # user's that is not valid UTF-8 reaches a record as a lone
    # surrogate, and that name is usually what the record is about.
    # `KeyboardInterrupt` is not a record refusing and goes on through.
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
        # Both `jri chat` and `jri view` configure logging, so two runs
        # of one session write to this file at once, and the rename a
        # rotation makes moves it out from under whichever run did not
        # make it: stamping, reading the size, rotating and appending
        # all happen under one lock.
        with self.file_lock:
            stamp = datetime.now(UTC).astimezone().strftime(LOG_TIME_FORMAT)[:-LOG_TIME_MICROSECOND_DIGITS]
            line = LOG_STAMP.format(time=stamp).encode() + body + b"\n"
            try:
                # The size of the file the open below will write to,
                # which is that file and never what a link standing on
                # its name points at.
                standing = self.file.lstat()
            except FileNotFoundError:
                size = 0
            else:
                # `O_NOFOLLOW` is what keeps the open below off a link
                # on this name, and Windows carries no such flag, so
                # there the `lstat` the size already costs is what
                # finds the link, and the failure it raises is what
                # brings the repair. `ELOOP` is the failure the flag
                # itself would have raised.
                if sys.platform == "win32" and stat.S_ISLNK(standing.st_mode):
                    raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), str(self.file))
                size = standing.st_size
            if size and size + len(line) > LOG_FILE_BYTES:
                self._rotate()
            with open(self.file, "ab", opener=_open_the_log) as stream:
                stream.write(line)

    def _rotate(self) -> None:
        opening, *window = self.kept_files
        # The first rotation is the one that would drop the front of
        # the session, so that is the file it freezes; every rotation
        # after it walks the window and leaves the opening alone. A
        # name holding anything but a file of the log's is a name
        # holding no records, so the rename goes ahead and fails, which
        # is what brings the repair.
        if not opening.is_file():
            self.file.replace(opening)
            return
        for older, newer in itertools.pairwise(window):
            if newer.is_file():
                newer.replace(older)


# Removing is best effort: an immutable file needs a privilege JRI does
# not have, and one path that will not go must leave the next one to be
# repaired anyway.
def _discard(path: Path) -> None:
    with contextlib.suppress(OSError):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def _grant_owner_access(path: Path) -> None:
    with contextlib.suppress(OSError):
        mode = path.stat().st_mode
        # A directory nothing may enter is a directory nothing may
        # write in, empty or read the rotated files out of. The mode
        # goes back only as far as the user the run belongs to, so what
        # was set for anybody else stands.
        wanted = mode | (stat.S_IRWXU if stat.S_ISDIR(mode) else stat.S_IRUSR | stat.S_IWUSR)
        if wanted != mode:
            path.chmod(wanted)


def _open_the_log(path: str, flags: int) -> int:
    return os.open(path, flags | LOG_FILE_FLAGS, LOG_FILE_PERMISSIONS)
