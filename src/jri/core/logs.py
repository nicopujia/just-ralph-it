import logging
from datetime import datetime

from . import paths
from .exceptions import PersistenceError
from .settings import Settings


def configure(settings: Settings) -> None:
    """Route this run's logs into a new file under the workspace.

    Raises:
        PersistenceError: If the log file cannot be created.
    """

    logs_dir = settings.cwd / paths.LOGS_DIR
    log_file = logs_dir / f"{datetime.now().astimezone().strftime('%Y-%m-%d_%H-%M-%S')}.log"
    try:
        logs_dir.mkdir(exist_ok=True, parents=True)
        handler = logging.FileHandler(log_file, encoding="utf-8")
    except OSError as error:
        raise PersistenceError(f"Could not create the log file `{log_file}`: {error.strerror}") from error
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"))
    application_logger = logging.getLogger("jri")
    application_logger.setLevel(settings.logging.level)
    application_logger.addHandler(handler)
    application_logger.propagate = False
