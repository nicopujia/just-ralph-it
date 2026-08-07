import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from jri import __version__
from jri.core import logs, paths
from jri.core.conversation import Conversation
from jri.core.exceptions import PersistenceError
from tests.doubles.openai import FakeClient
from tests.doubles.settings import build_settings
from tests.doubles.workspace import install_workspace

# Big enough to fill the files below in few records, and small enough
# that no record is the one the bound cannot hold.
FILLING_RECORD_BYTES = 64 * 1024


def test_appends_a_run_to_the_log_the_session_already_has(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([])).model_copy(update={"logging": SimpleNamespace(level="INFO")})
    (tmp_path / paths.LOG_FILE).write_text("[an earlier run] left this\n")

    logs.configure(settings)
    Conversation(settings)

    files = list((tmp_path / paths.LOGS_DIR).iterdir())
    assert [file.name for file in files] == [Path(paths.LOG_FILE).name]
    written = files[0].read_text()
    assert "[an earlier run] left this" in written
    assert "initialized" in written


def test_names_the_version_and_the_process_on_every_line(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([])).model_copy(update={"logging": SimpleNamespace(level="INFO")})

    logs.configure(settings)
    Conversation(settings)

    lines = (tmp_path / paths.LOG_FILE).read_text().splitlines()
    assert lines
    assert all(f"[{__version__}] [{os.getpid()}]" in line for line in lines)


def test_bounds_the_files_and_the_bytes_a_long_session_leaves(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([])).model_copy(update={"logging": SimpleNamespace(level="INFO")})

    logs.configure(settings)
    logger = logging.getLogger("jri")
    for _ in range(logs.LOG_FILE_BYTES // FILLING_RECORD_BYTES * (logs.KEPT_LOG_FILES + 1)):
        logger.info("x" * FILLING_RECORD_BYTES)

    files = list((tmp_path / paths.LOGS_DIR).iterdir())
    assert len(files) == logs.KEPT_LOG_FILES
    assert all(file.stat().st_size <= logs.LOG_FILE_BYTES for file in files)


def test_explains_when_the_log_file_cannot_be_created(tmp_path: Path) -> None:
    (tmp_path / paths.WORKSPACE_DIR).mkdir()
    (tmp_path / paths.LOGS_DIR).write_text("not a directory")

    with pytest.raises(PersistenceError, match="Could not create the log file"):
        logs.configure(build_settings(FakeClient([])))
