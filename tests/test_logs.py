import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from jri import __version__
from jri.core import logs, paths
from jri.core.conversation import Conversation
from jri.core.exceptions import PersistenceError
from tests.doubles.logs import list_log_files, read_session_log, run_beside
from tests.doubles.openai import FakeClient
from tests.doubles.settings import build_settings
from tests.doubles.workspace import install_workspace

EARLIER_RUN = "[an earlier run] left this"
# Big enough to fill the files below in few records, and small enough
# that no record is the one the bound cannot hold.
FILLING_RECORD_BYTES = 64 * 1024
# Wide enough that the records two runs write pass the bound below
# without reaching twice it, so exactly one rotation happens and every
# record either run wrote is still there to read.
RECORD_PADDING = "x" * 200
RECORDS_PER_RUN = 200
SMALL_LOG_FILE_BYTES = 64 * 1024
TURN_RECORDS = 3
TURNS = 2


def test_appends_a_run_to_the_log_the_session_already_has(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([])).model_copy(update={"logging": SimpleNamespace(level="INFO")})
    (tmp_path / paths.LOG_FILE).write_text(f"{EARLIER_RUN}\n")

    logs.configure(settings)
    Conversation(settings)

    files = list_log_files(tmp_path)
    assert [file.name for file in files] == [Path(paths.LOG_FILE).name]
    written = files[0].read_text()
    assert EARLIER_RUN in written
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

    files = list_log_files(tmp_path)
    assert len(files) == logs.KEPT_LOG_FILES
    assert all(file.stat().st_size <= logs.LOG_FILE_BYTES for file in files)


def test_reads_back_in_the_order_two_runs_of_a_session_wrote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([])).model_copy(update={"logging": SimpleNamespace(level="INFO")})
    monkeypatch.setattr(logs, "LOG_FILE_BYTES", SMALL_LOG_FILE_BYTES)
    # A file already at the bound, so the first record either run
    # writes rotates it and both are rotating the one file.
    filler = "." * (SMALL_LOG_FILE_BYTES - len(EARLIER_RUN) - 1)
    (tmp_path / paths.LOG_FILE).write_text(f"{EARLIER_RUN}{filler}\n")
    logs.configure(settings)
    logger = logging.getLogger("jri.chat")
    written: list[str] = []

    with run_beside(tmp_path, bound=SMALL_LOG_FILE_BYTES, batches=[TURN_RECORDS] * TURNS) as turns:
        for turn in range(TURNS):
            for index in range(TURN_RECORDS):
                logger.info("CHAT %d %d", turn, index)
                written.append(f"CHAT {turn} {index}")
            turns.start(turn)
            turns.wait_for(turn)
            written += [f"VIEW {turn} {index}" for index in range(TURN_RECORDS)]

    log = read_session_log(tmp_path)
    assert EARLIER_RUN in log, "a rotation the runs raced dropped the file neither of them had filled"
    assert re.findall(r"(?:CHAT|VIEW) \d+ \d+", log) == written


def test_keeps_every_record_two_runs_of_a_session_write_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([])).model_copy(update={"logging": SimpleNamespace(level="INFO")})
    monkeypatch.setattr(logs, "LOG_FILE_BYTES", SMALL_LOG_FILE_BYTES)
    logs.configure(settings)
    logger = logging.getLogger("jri.chat")

    with run_beside(tmp_path, bound=SMALL_LOG_FILE_BYTES, batches=[RECORDS_PER_RUN], padding=RECORD_PADDING) as turns:
        turns.start(0)
        for index in range(RECORDS_PER_RUN):
            logger.info("CHAT 0 %d %s", index, RECORD_PADDING)
        turns.wait_for(0)

    log = read_session_log(tmp_path)
    for run in ("CHAT", "VIEW"):
        assert re.findall(rf"{run} 0 (\d+)", log) == [str(index) for index in range(RECORDS_PER_RUN)]


def test_explains_when_the_log_file_cannot_be_created(tmp_path: Path) -> None:
    (tmp_path / paths.WORKSPACE_DIR).mkdir()
    (tmp_path / paths.LOGS_DIR).write_text("not a directory")

    with pytest.raises(PersistenceError, match="Could not create the log file"):
        logs.configure(build_settings(FakeClient([])))
