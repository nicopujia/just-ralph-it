import logging
from logging.handlers import RotatingFileHandler

from jri import __version__

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


def configure(settings: Settings) -> None:
    workspace = Workspace.find()
    log_file = workspace.log_file
    try:
        workspace.logs_dir.mkdir(exist_ok=True, parents=True)
        handler = RotatingFileHandler(
            log_file, maxBytes=LOG_FILE_BYTES, backupCount=KEPT_LOG_FILES - 1, encoding="utf-8"
        )
    except OSError as error:
        raise PersistenceError(f"Could not create the log file `{log_file}`: {error.strerror}") from error
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
