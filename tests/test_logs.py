import itertools
import logging
import os
import re
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from jri import __version__
from jri.core import logs, paths
from jri.core.conversation import Conversation
from jri.core.exceptions import PersistenceError
from tests.doubles.logs import (
    LOG_PATHS,
    SABOTAGE_SHAPES,
    Exploding,
    list_log_files,
    read_session_log,
    read_user_files,
    run_beside,
    sabotage,
)
from tests.doubles.openai import FakeClient
from tests.doubles.settings import build_settings
from tests.doubles.workspace import install_workspace

EARLIER_RUN = "[an earlier run] left this"
FAILURE_RECORD = "THE BUG HAPPENED HERE"
# Big enough to fill the files below in few records, and inside the
# bound a record has, so what fills them are whole records.
FILLING_RECORD_BYTES = 32 * 1024
# What `os.fsdecode` hands back for a byte no UTF-8 decoding claims,
# and what `jri.lib.git` therefore hands a record for a repository
# holding a file or a ref whose name is not valid UTF-8.
LONE_SURROGATE_NAME = "refs/heads/caf\udce9.lock"
OPENING_RECORD = "THE SESSION OPENED HERE"
# Past the bound a record has, so the second run spends the
# milliseconds its formatting and its truncation cost between making a
# record and taking the lock -- the window records made later land in.
OVERSIZED_PADDING = "y" * (logs.LOG_RECORD_BYTES * 4)
OVERSIZED_RECORDS = 40
# Wide enough that the records two runs write pass the bound below
# without reaching twice it, so exactly one rotation happens and every
# record either run wrote is still there to read.
RECORD_PADDING = "x" * 200
RECORDS_PER_RUN = 200
# Every path the log needs, crossed with every state an unprivileged
# process can leave on one. What the cross leaves out, and why, is on
# `SABOTAGE_SHAPES`.
SABOTAGED_PATHS = tuple(itertools.product(LOG_PATHS, SABOTAGE_SHAPES))
# The three of those the log has no way to tell from its own paths.
# Nothing fails in any of them, so no repair ever comes, and what the
# run writes -- its records in two of them, its lock file in the third
# -- lands outside `.jri`. Each is a defect and not a decision: the
# open on the log's own file carries `O_NOFOLLOW` for exactly this
# reason, and these are the three ways around it.
SABOTAGED_PATHS_THAT_ESCAPE = {
    (paths.LOGS_DIR, "a link to a directory"): "a link the log needs no repair to follow is a link it keeps",
    (paths.LOG_FILE, "a hard link"): "a second name for the user's file is a file, and `lstat` says so",
    (paths.LOG_LOCK_FILE, "a link one write away"): "`jri.lib.lock` opens the lock without `O_NOFOLLOW`",
}
# The same cross, with those three marked. The marker is strict, so
# containing one of them turns this red rather than passing quietly,
# and the fix is what takes it off the list above.
SABOTAGED_PATHS_TO_CONTAIN = tuple(
    pytest.param(
        path,
        shape,
        marks=[pytest.mark.xfail(strict=True, reason=SABOTAGED_PATHS_THAT_ESCAPE[path, shape])]
        if (path, shape) in SABOTAGED_PATHS_THAT_ESCAPE
        else [],
    )
    for path, shape in SABOTAGED_PATHS
)
SMALL_LOG_FILE_BYTES = 64 * 1024
# What the first run writes for as long as the second is busy with the
# oversized ones, so the two of them are on the lock together.
SMALL_RECORDS = 2000
STAMP = re.compile(r"^\[([\d-]+ [\d:,]+)\]", re.MULTILINE)
TURN_RECORDS = 3
TURNS = 2
# A pipe nobody reads and a lock nobody drops both answer a write by
# never coming back, so the sabotaged runs below make their records
# from a thread they outlive, and a run that never comes back reads as
# a failure rather than as a suite that stopped.
WRITE_SECONDS = 10


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


def test_keeps_the_opening_of_a_session_that_fills_the_files_over_and_over(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([])).model_copy(update={"logging": SimpleNamespace(level="INFO")})

    logs.configure(settings)
    logger = logging.getLogger("jri")
    logger.info(OPENING_RECORD)
    for _ in range(logs.LOG_FILE_BYTES // FILLING_RECORD_BYTES * (logs.KEPT_LOG_FILES + 1)):
        logger.info("x" * FILLING_RECORD_BYTES)
    logger.info(FAILURE_RECORD)

    log = read_session_log(tmp_path)
    assert OPENING_RECORD in log
    assert FAILURE_RECORD in log


@pytest.mark.parametrize(("path", "shape"), SABOTAGED_PATHS)
def test_writes_on_when_a_path_the_log_needs_is_not_what_it_must_be(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, shape: str
) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([])).model_copy(update={"logging": SimpleNamespace(level="INFO")})
    monkeypatch.setattr(logs, "LOG_FILE_BYTES", SMALL_LOG_FILE_BYTES)
    logs.configure(settings)
    logger = logging.getLogger("jri")
    logger.info(OPENING_RECORD)

    try:
        sabotage(tmp_path, path, shape)
    except OSError as error:
        pytest.skip(f"this machine withholds what the sabotage needs: {error}")
    writing = threading.Thread(target=_fill_past_the_bound, args=(logger,), daemon=True)
    writing.start()
    writing.join(WRITE_SECONDS)

    assert not writing.is_alive(), "an open on a name nobody answers for never came back, and the lock went with it"
    # Reading the directory back is itself the assertion that no name
    # the log rotates through still holds something else.
    assert FAILURE_RECORD in read_session_log(tmp_path)


# `O_NOFOLLOW` is what holds a link off the log's own name, and
# Windows leaves the flag out, so what a link there does is what
# nothing here has run.
@pytest.mark.skipif(
    sys.platform == "win32", reason="a link on the log's own name is followed where `O_NOFOLLOW` is not"
)
@pytest.mark.parametrize(("path", "shape"), SABOTAGED_PATHS_TO_CONTAIN)
def test_writes_nothing_outside_the_workspace_directory_when_a_path_the_log_needs_is_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, shape: str
) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([])).model_copy(update={"logging": SimpleNamespace(level="INFO")})
    monkeypatch.setattr(logs, "LOG_FILE_BYTES", SMALL_LOG_FILE_BYTES)
    logs.configure(settings)
    logger = logging.getLogger("jri")

    try:
        sabotage(tmp_path, path, shape)
    except OSError as error:
        pytest.skip(f"this machine withholds what the sabotage needs: {error}")
    planted = read_user_files(tmp_path)
    writing = threading.Thread(target=_fill_past_the_bound, args=(logger,), daemon=True)
    writing.start()
    writing.join(WRITE_SECONDS)

    assert read_user_files(tmp_path) == planted


def test_keeps_the_records_before_one_longer_than_the_whole_file(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([])).model_copy(update={"logging": SimpleNamespace(level="INFO")})

    logs.configure(settings)
    logger = logging.getLogger("jri")
    logger.info("THE BUG HAPPENED HERE")
    # What `Explorer.fetch_web_page` logs at DEBUG: the page it read
    # whole, bounded only by the bytes the explorer accepts.
    logger.info("fetch_response response_body=%r", "z" * (logs.LOG_FILE_BYTES * 2))

    files = list_log_files(tmp_path)
    assert all(file.stat().st_size <= logs.LOG_FILE_BYTES for file in files)
    log = read_session_log(tmp_path)
    assert "THE BUG HAPPENED HERE" in log
    written = next(line for line in log.splitlines() if "fetch_response" in line)
    assert len(written.encode()) <= logs.LOG_RECORD_BYTES
    notice = re.search(r"\[(\d+) bytes dropped\]", written)
    assert notice
    # What the line says went is measured against the record it was
    # cut out of, not against what is left of it.
    assert len(written.encode()) + int(notice.group(1)) > logs.LOG_FILE_BYTES * 2


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


def test_reads_back_in_time_order_when_two_runs_write_at_once(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([])).model_copy(update={"logging": SimpleNamespace(level="INFO")})
    logs.configure(settings)
    logger = logging.getLogger("jri.chat")

    with run_beside(
        tmp_path, bound=logs.LOG_FILE_BYTES, batches=[OVERSIZED_RECORDS], padding=OVERSIZED_PADDING
    ) as turns:
        turns.start(0)
        for index in range(SMALL_RECORDS):
            logger.info("CHAT 0 %d", index)
        turns.wait_for(0)

    log = read_session_log(tmp_path)
    assert len(re.findall(r"VIEW 0 \d+", log)) == OVERSIZED_RECORDS
    stamps = STAMP.findall(log)
    assert len(stamps) == SMALL_RECORDS + OVERSIZED_RECORDS
    assert stamps == sorted(stamps), "a record reached the file behind one stamped after it"


def test_writes_on_when_a_record_cannot_be_rendered(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([])).model_copy(update={"logging": SimpleNamespace(level="INFO")})
    logs.configure(settings)
    logger = logging.getLogger("jri.chat")

    logger.info("turn_finished ending=%d", "cancelled")
    logger.warning("read_finished notes=%(count)d", {"notes": 1})
    logger.error("output=%s", Exploding())
    logger.info(FAILURE_RECORD)

    log = read_session_log(tmp_path)
    markers = [line for line in log.splitlines() if "unrendered_record" in line]
    assert [line.split("] [")[3] for line in markers] == ["INFO", "WARNING", "ERROR"]
    assert all(f"[{__version__}] [{os.getpid()}]" in line for line in markers)
    assert all("[jri.chat] unrendered_record source=test_logs.py:" in line for line in markers)
    assert FAILURE_RECORD in log


def test_keeps_a_name_that_will_not_encode_as_the_escapes_it_is_written_in(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([])).model_copy(update={"logging": SimpleNamespace(level="INFO")})
    logs.configure(settings)
    logger = logging.getLogger("jri.chat")

    logger.info("git_lock_released path=%s", LONE_SURROGATE_NAME)

    log = read_session_log(tmp_path)
    assert "path=refs/heads/caf\\udce9.lock" in log
    assert "unrendered_record" not in log


def test_explains_when_the_log_file_cannot_be_created(tmp_path: Path) -> None:
    # The workspace directory holds the notebook, the configuration and
    # the specifications, so what stands on that name is not the log's
    # to clear the way it clears its own.
    (tmp_path / paths.WORKSPACE_DIR).write_text("not a directory")

    with pytest.raises(PersistenceError, match="Could not create the log file"):
        logs.configure(build_settings(FakeClient([])))


# The same name, taken while the session is already running: the log
# clears what stands on the paths under it, and this one is not its
# to clear, so the records go. The run is what must not go with them.
def test_costs_the_records_and_not_the_run_when_a_file_stands_on_the_workspace_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([])).model_copy(update={"logging": SimpleNamespace(level="INFO")})
    logs.configure(settings)
    logger = logging.getLogger("jri")
    shutil.rmtree(tmp_path / paths.WORKSPACE_DIR)
    (tmp_path / paths.WORKSPACE_DIR).write_text("what the user left where `.jri` was", encoding="utf-8")

    logger.info(OPENING_RECORD)

    # The terminal is a `jri chat` screen's, so a record the log
    # cannot write is dropped rather than reported on it.
    assert capsys.readouterr() == ("", "")
    (tmp_path / paths.WORKSPACE_DIR).unlink()
    logger.info(FAILURE_RECORD)
    assert FAILURE_RECORD in read_session_log(tmp_path)


# Past the bound the sabotaged runs set, so the record that lands last
# has been through a rename as well as through an append. A pipe
# nobody reads and a lock nobody drops both answer by never coming
# back, so this is run from a thread the test outlives.
def _fill_past_the_bound(logger: logging.Logger) -> None:
    for _ in range(SMALL_LOG_FILE_BYTES // FILLING_RECORD_BYTES + 1):
        logger.info("x" * FILLING_RECORD_BYTES)
    logger.info(FAILURE_RECORD)
