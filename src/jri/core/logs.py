import contextlib
import itertools
import logging
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
KEPT_LOG_FILES = 2
LOG_FILE_BYTES = 5 * 1024 * 1024
# A record reaches the file whole or not at all, so one longer than the
# file bound would leave a file past that bound and take every record
# before it down on the rotation the next one makes. What grows this
# far is a page fetched, a file read or a model's output logged at
# DEBUG, and it is the front of such a record that names what happened,
# so the rest goes and what went is counted on the line. This must stay
# under `LOG_FILE_BYTES` for the bound over the files to hold.
LOG_RECORD_BYTES = 64 * 1024
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
    handler.setFormatter(
        logging.Formatter(f"[%(asctime)s] [{__version__}] [%(process)d] [%(levelname)s] [%(name)s] %(message)s")
    )
    application_logger = logging.getLogger("jri")
    application_logger.setLevel(settings.logging.level)
    application_logger.addHandler(handler)
    application_logger.propagate = False


class SessionLog(logging.Handler):
    def __init__(self, file: Path, lock_file: Path) -> None:
        super().__init__()
        self.file = file
        # Oldest first, so rotation walks the pairs in order and the
        # file falling off the end is the one the first rename lands on.
        self.kept_files = tuple(
            file.with_name(f"{file.name}.{index}") if index else file for index in reversed(range(KEPT_LOG_FILES))
        )
        self.file_lock = Lock(lock_file)

    @override
    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record).encode()
        if len(line) > LOG_RECORD_BYTES:
            # The notice takes its own room out of the bound, and the
            # widest count it can carry is the length it is measured
            # against, so what is left is never past the bound.
            room = LOG_RECORD_BYTES - len(TRUNCATION_NOTICE.format(dropped=len(line)))
            kept = line[:room].decode("utf-8", errors="ignore").encode()
            dropped = len(line) - len(kept)
            line = kept + TRUNCATION_NOTICE.format(dropped=dropped).encode()
        line += b"\n"
        # Both `jri chat` and `jri view` configure logging, so two runs
        # of one session write to this file at once, and the rename a
        # rotation makes moves it out from under whichever run did not
        # make it: reading the size, rotating and appending all happen
        # under one lock. A record that cannot be written is dropped
        # rather than reported, since the stream `logging` reports on
        # is the terminal a `jri chat` screen holds.
        with contextlib.suppress(OSError, LockError), self.file_lock:
            size = self.file.stat().st_size if self.file.exists() else 0
            if size and size + len(line) > LOG_FILE_BYTES:
                self._rotate()
            with self.file.open("ab") as stream:
                stream.write(line)

    def _rotate(self) -> None:
        for older, newer in itertools.pairwise(self.kept_files):
            if newer.exists():
                newer.replace(older)
