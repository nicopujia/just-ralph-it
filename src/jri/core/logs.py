import contextlib
import itertools
import logging
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


def configure(settings: Settings) -> None:
    workspace = Workspace.find()
    log_file = workspace.log_file
    try:
        workspace.logs_dir.mkdir(exist_ok=True, parents=True)
        # A log nothing may write to is worth saying at the start,
        # since every record after this is dropped rather than reported.
        log_file.touch()
    except OSError as error:
        raise PersistenceError(f"Could not create the log file `{log_file}`: {error.strerror}") from error
    handler = SessionLog(log_file, workspace.log_lock_file)
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
        body = self.format(record).encode()
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
        # notice a directory that went, and `jri init --force` beside a
        # running session takes this one: a run whose records all land
        # inside it is silent from there until somebody else happens to
        # put it back. It puts the directory back itself instead, and
        # asks before making it, since one that is there answers a
        # `stat` and costs a raised `FileExistsError` every record.
        with contextlib.suppress(OSError, LockError):
            if not self.file.parent.exists():
                self.file.parent.mkdir(parents=True, exist_ok=True)
            # Both `jri chat` and `jri view` configure logging, so two
            # runs of one session write to this file at once, and the
            # rename a rotation makes moves it out from under whichever
            # run did not make it: stamping, reading the size, rotating
            # and appending all happen under one lock.
            with self.file_lock:
                stamp = datetime.now(UTC).astimezone().strftime(LOG_TIME_FORMAT)[:-LOG_TIME_MICROSECOND_DIGITS]
                line = LOG_STAMP.format(time=stamp).encode() + body + b"\n"
                size = self.file.stat().st_size if self.file.exists() else 0
                if size and size + len(line) > LOG_FILE_BYTES:
                    self._rotate()
                with self.file.open("ab") as stream:
                    stream.write(line)

    def _rotate(self) -> None:
        opening, *window = self.kept_files
        # The first rotation is the one that would drop the front of
        # the session, so that is the file it freezes; every rotation
        # after it walks the window and leaves the opening alone.
        if not opening.exists():
            self.file.replace(opening)
            return
        for older, newer in itertools.pairwise(window):
            if newer.exists():
                newer.replace(older)
