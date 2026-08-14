import itertools
import logging
import os
import re
import shutil
import threading
from pathlib import Path

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

AT_ONCE_TURN_RECORDS = 25
AT_ONCE_TURNS = 8
EARLIER_RUN = "[an earlier run] left this"
FAILURE_RECORD = "THE BUG HAPPENED HERE"
FILLED_TIMES = 4
FILLER_LINE_BYTES = 1024
FILLING_RECORD_BYTES = 32 * 1024
# A lone surrogate is how Python represents a git ref byte sequence that is not valid UTF-8 (`surrogateescape`).
LONE_SURROGATE_NAME = "refs/heads/caf\udce9.lock"
OPENING_RECORD = "THE SESSION OPENED HERE"
OVERSIZED_PADDING = "y" * (logs.RECORD_BYTES * 4)
OVERSIZED_RECORDS = 40
RECORD_PADDING = "x" * 200
SABOTAGED_PATHS = tuple(itertools.product(LOG_PATHS, SABOTAGE_SHAPES))
SABOTAGED_PATHS_THAT_ESCAPE = {
    (paths.LOGS_DIR, "a link to a directory"): "a link the log needs no repair to follow is a link it keeps",
    (paths.LOG_FILE, "a hard link"): "a second name for the user's file is a file, and `lstat` says so",
    (paths.LOG_LOCK_FILE, "a link one write away"): "`jri.lib.lock` opens the lock without `O_NOFOLLOW`",
}
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
SMALL_FILE_BYTES = 64 * 1024
SMALL_RECORDS = 2000
STAMP = re.compile(r"^\[([\d-]+ [\d:,]+)\]", re.MULTILINE)
TURN_RECORDS = 3
TURNS = 2
WRITE_SECONDS = 10


def test_appends_a_run_to_the_log_the_session_already_has(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([]), level="INFO")
    (tmp_path / paths.LOG_FILE).write_text(f"{EARLIER_RUN}\n")

    logs.configure(settings)
    logging.getLogger("jri").info(OPENING_RECORD)

    files = list_log_files(tmp_path)
    assert [file.name for file in files] == [Path(paths.LOG_FILE).name]
    written = files[0].read_text()
    assert EARLIER_RUN in written
    assert OPENING_RECORD in written


def test_names_the_version_and_the_process_on_every_line(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([]), level="INFO")

    logs.configure(settings)
    Conversation(settings)

    lines = (tmp_path / paths.LOG_FILE).read_text().splitlines()
    assert lines
    assert all(f"[{__version__}] [{os.getpid()}]" in line for line in lines)


def test_bounds_the_file_and_the_bytes_a_long_session_leaves(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([]), level="INFO")

    logs.configure(settings)
    logger = logging.getLogger("jri")
    for _ in range(logs.FILE_BYTES // FILLING_RECORD_BYTES * FILLED_TIMES):
        logger.info("x" * FILLING_RECORD_BYTES)

    files = list_log_files(tmp_path)
    assert [file.name for file in files] == [Path(paths.LOG_FILE).name]
    assert files[0].stat().st_size <= logs.FILE_BYTES


def test_keeps_the_newest_records_of_a_session_that_fills_the_file_over_and_over(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([]), level="INFO")

    logs.configure(settings)
    logger = logging.getLogger("jri")
    logger.info(OPENING_RECORD)
    for _ in range(logs.FILE_BYTES // FILLING_RECORD_BYTES * FILLED_TIMES):
        logger.info("x" * FILLING_RECORD_BYTES)
    logger.info(FAILURE_RECORD)

    log = read_session_log(tmp_path)
    assert FAILURE_RECORD in log
    assert OPENING_RECORD not in log, "the file keeps its newest records, and this one is older than the limit"
    assert logs.TRIM_NOTICE in log


@pytest.mark.parametrize(("path", "shape"), SABOTAGED_PATHS)
def test_writes_on_when_a_path_the_log_needs_is_not_what_it_must_be(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, shape: str
) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([]), level="INFO")
    monkeypatch.setattr(logs, "FILE_BYTES", SMALL_FILE_BYTES)
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
    assert FAILURE_RECORD in read_session_log(tmp_path)


@pytest.mark.parametrize(("path", "shape"), SABOTAGED_PATHS_TO_CONTAIN)
def test_writes_nothing_outside_the_workspace_directory_when_a_path_the_log_needs_is_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, shape: str
) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([]), level="INFO")
    monkeypatch.setattr(logs, "FILE_BYTES", SMALL_FILE_BYTES)
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
    settings = build_settings(FakeClient([]), level="INFO")

    logs.configure(settings)
    logger = logging.getLogger("jri")
    logger.info("THE BUG HAPPENED HERE")
    # A fetched response is a realistic source of a record this large: a call can return an oversized body in one line.
    logger.info("fetch_response response_body=%r", "z" * (logs.FILE_BYTES * 2))

    files = list_log_files(tmp_path)
    assert all(file.stat().st_size <= logs.FILE_BYTES for file in files)
    log = read_session_log(tmp_path)
    assert "THE BUG HAPPENED HERE" in log
    written = next(line for line in log.splitlines() if "fetch_response" in line)
    assert len(written.encode()) <= logs.RECORD_BYTES
    notice = re.search(r"\[(\d+) bytes dropped\]", written)
    assert notice
    # Confirm the dropped count is honest, not merely present, by checking it against the record's real size.
    assert len(written.encode()) + int(notice.group(1)) > logs.FILE_BYTES * 2


def test_reads_back_in_the_order_two_runs_of_a_session_wrote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([]), level="INFO")
    monkeypatch.setattr(logs, "FILE_BYTES", SMALL_FILE_BYTES)
    # Fill the file to its bound so the first records the runs write force a trim, instead of waiting for one.
    # A trim keeps the newest records, so write the record that must survive it last.
    filler = f"{'.' * (FILLER_LINE_BYTES - 1)}\n" * (SMALL_FILE_BYTES // FILLER_LINE_BYTES)
    (tmp_path / paths.LOG_FILE).write_text(f"{filler}{EARLIER_RUN}\n")
    logs.configure(settings)
    logger = logging.getLogger("jri.chat")
    written: list[str] = []

    with run_beside(tmp_path, bound=SMALL_FILE_BYTES, batches=[TURN_RECORDS] * TURNS) as turns:
        for turn in range(TURNS):
            for index in range(TURN_RECORDS):
                logger.info("CHAT %d %d", turn, index)
                written.append(f"CHAT {turn} {index}")
            turns.start(turn)
            turns.wait_for(turn)
            written += [f"VIEW {turn} {index}" for index in range(TURN_RECORDS)]

    log = read_session_log(tmp_path)
    assert EARLIER_RUN in log, "a trim the runs raced dropped a record newer than the part it keeps"
    assert re.findall(r"(?:CHAT|VIEW) \d+ \d+", log) == written


def test_keeps_every_record_after_the_oldest_when_two_runs_of_a_session_write_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([]), level="INFO")
    monkeypatch.setattr(logs, "FILE_BYTES", SMALL_FILE_BYTES)
    logs.configure(settings)
    logger = logging.getLogger("jri.chat")
    # The runs write in turns. Each run starts its turn while the other run writes.
    # One run can write two turns before the other run writes. It cannot write more.
    # The records of two turns are less than the part a trim keeps, so a trim keeps records of both runs.
    batches = [AT_ONCE_TURN_RECORDS] * AT_ONCE_TURNS

    with run_beside(tmp_path, bound=SMALL_FILE_BYTES, batches=batches, padding=RECORD_PADDING) as turns:
        for turn in range(AT_ONCE_TURNS):
            turns.start(turn)
            for index in range(AT_ONCE_TURN_RECORDS):
                logger.info("CHAT %d %d %s", turn, index, RECORD_PADDING)
            turns.wait_for(turn)

    log = read_session_log(tmp_path)
    for run in ("CHAT", "VIEW"):
        written = [f"{run} {turn} {index}" for turn in range(AT_ONCE_TURNS) for index in range(AT_ONCE_TURN_RECORDS)]
        kept = re.findall(rf"{run} \d+ \d+", log)
        assert kept, f"the trims dropped every record the {run} run wrote"
        # A trim drops the oldest records. What stays is every record written after them, in write order.
        assert kept == written[written.index(kept[0]) :]


def test_reads_back_in_time_order_when_two_runs_write_at_once(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([]), level="INFO")
    logs.configure(settings)
    logger = logging.getLogger("jri.chat")

    with run_beside(tmp_path, bound=logs.FILE_BYTES, batches=[OVERSIZED_RECORDS], padding=OVERSIZED_PADDING) as turns:
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
    settings = build_settings(FakeClient([]), level="INFO")
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
    settings = build_settings(FakeClient([]), level="INFO")
    logs.configure(settings)
    logger = logging.getLogger("jri.chat")

    logger.info("git_lock_released path=%s", LONE_SURROGATE_NAME)

    log = read_session_log(tmp_path)
    assert "path=refs/heads/caf\\udce9.lock" in log
    assert "unrendered_record" not in log


def test_explains_when_the_log_file_cannot_be_created(tmp_path: Path) -> None:
    (tmp_path / paths.WORKSPACE_DIR).write_text("not a directory")

    with pytest.raises(PersistenceError, match="Could not create the log file"):
        logs.configure(build_settings(FakeClient([])))


def test_costs_the_records_and_not_the_run_when_a_file_stands_on_the_workspace_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_workspace(tmp_path)
    settings = build_settings(FakeClient([]), level="INFO")
    logs.configure(settings)
    logger = logging.getLogger("jri")
    shutil.rmtree(tmp_path / paths.WORKSPACE_DIR)
    (tmp_path / paths.WORKSPACE_DIR).write_text("what the user left where `.jri` was", encoding="utf-8")

    logger.info(OPENING_RECORD)

    # `jri chat` owns the terminal and redraws it live. Writing anything here would corrupt that screen.
    assert capsys.readouterr() == ("", "")
    (tmp_path / paths.WORKSPACE_DIR).unlink()
    logger.info(FAILURE_RECORD)
    assert FAILURE_RECORD in read_session_log(tmp_path)


def _fill_past_the_bound(logger: logging.Logger) -> None:
    for _ in range(SMALL_FILE_BYTES // FILLING_RECORD_BYTES + 1):
        logger.info("x" * FILLING_RECORD_BYTES)
    logger.info(FAILURE_RECORD)
