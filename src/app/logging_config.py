"""Unified logging configuration. Call setup_logging() once at app startup."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGS_DIR = Path(__file__).resolve().parent.parent.parent / ".jri" / "logs"
APP_LOG = LOGS_DIR / "app.log"

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
BACKUP_COUNT = 3


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger with rotating file + console handlers."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    # Rotating file handler
    file_handler = RotatingFileHandler(
        APP_LOG, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    # Console handler (for dev)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
