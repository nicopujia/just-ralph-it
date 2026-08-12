import contextlib
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, override

from jri.core import paths

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
LOG_PATHS = (paths.LOGS_DIR, paths.LOG_LOCK_FILE, paths.LOG_FILE)
POLL = 0.01
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
SABOTAGE_SHAPES = (
    "gone",
    "a file",
    "a directory",
    "a link to a file",
    "a link to a directory",
    "a link going nowhere",
    "a link one write away",
    "a link to itself",
    "a hard link",
    "nothing may use",
    "nothing may write",
    # Check this test support.
    # Check this test support.
    # Check this test support.
    *(() if sys.platform == "win32" else ("a pipe", "a socket")),
)
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
SECOND_RUN = """
import logging, sys, time
from pathlib import Path
from types import SimpleNamespace

from jri.core import logs

bound, turns = int(sys.argv[1]), Path(sys.argv[2])
padding = Path(sys.argv[4]).read_text(encoding="utf-8")
logs.LOG_FILE_BYTES = bound
logs.configure(SimpleNamespace(logging=SimpleNamespace(level="INFO")))
logger = logging.getLogger("jri.view")
(turns / "configured").touch()
for turn, size in enumerate(int(batch) for batch in sys.argv[3].split(",")):
    while not (turns / str(turn)).exists():
        time.sleep(0.01)
    for index in range(size):
        logger.info("VIEW %d %d %s", turn, index, padding)
    (turns / f"{turn}.done").touch()
logging.shutdown()
"""
# Check this test support.
TIMEOUT = 30
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
UNGUARDED_SOCKET = socket.socket
# Check this test support.
# Check this test support.
# Check this test support.
USER_FILES_DIR = "src"


# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
def list_log_files(workspace: Path) -> list[Path]:
    return sorted(
        (
            file
            for file in (workspace / paths.LOGS_DIR).glob(f"{Path(paths.LOG_FILE).name}*")
            if not file.is_symlink() and file.is_file()
        ),
        reverse=True,
    )


# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
def read_session_log(workspace: Path) -> str:
    _grant_access(workspace / paths.LOGS_DIR, stat.S_IRWXU)
    files = list_log_files(workspace)
    for file in files:
        _grant_access(file, stat.S_IRUSR)
    return "".join(file.read_text(encoding="utf-8") for file in files)


# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
def read_user_files(workspace: Path) -> dict[str, int]:
    return {
        str(path.relative_to(workspace)): path.lstat().st_size
        for path in workspace.rglob("*")
        if not path.is_relative_to(workspace / paths.WORKSPACE_DIR) and (path.is_symlink() or not path.is_dir())
    }


@contextmanager
def run_beside(workspace: Path, *, bound: int, batches: "Sequence[int]", padding: str = "") -> "Iterator[Turns]":
    turns = workspace / "turns"
    turns.mkdir()
    # Check this test support.
    # Check this test support.
    # Check this test support.
    # Check this test support.
    padding_file = workspace / "padding"
    padding_file.write_text(padding, encoding="utf-8")
    arguments = (str(bound), str(turns), ",".join(str(batch) for batch in batches), str(padding_file))
    run = subprocess.Popen(
        [sys.executable, "-c", SECOND_RUN, *arguments], cwd=workspace, stderr=subprocess.PIPE, text=True
    )
    reported = ""
    try:
        _wait_for(turns / "configured")
        yield Turns(turns)
        _, reported = run.communicate(timeout=TIMEOUT)
    finally:
        run.kill()
        run.wait()
    # Check this test support.
    # Check this test support.
    assert not reported, f"the second run wrote this to the terminal:\n{reported}"
    assert run.returncode == 0


# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
def sabotage(workspace: Path, path: str, shape: str) -> None:
    sabotaged = workspace / path
    a_directory = workspace / USER_FILES_DIR / "notes"
    a_file = workspace / USER_FILES_DIR / "main.py"
    a_directory.mkdir(parents=True, exist_ok=True)
    a_file.write_text("what the user wrote\n", encoding="utf-8")
    match shape:
        case "gone":
            _clear(sabotaged)
        case "a file":
            _clear(sabotaged)
            sabotaged.write_text("not what the log needs", encoding="utf-8")
        case "a directory":
            _clear(sabotaged)
            sabotaged.mkdir()
        case "a link to a file":
            _clear(sabotaged)
            sabotaged.symlink_to(a_file)
        case "a link to a directory":
            _clear(sabotaged)
            sabotaged.symlink_to(a_directory)
        case "a link going nowhere":
            _clear(sabotaged)
            sabotaged.symlink_to(workspace / "nowhere" / sabotaged.name)
        case "a link one write away":
            _clear(sabotaged)
            sabotaged.symlink_to(a_directory / sabotaged.name)
        case "a link to itself":
            _clear(sabotaged)
            sabotaged.symlink_to(sabotaged)
        case "a hard link":
            _clear(sabotaged)
            sabotaged.hardlink_to(a_file)
        # Check this test support.
        # Check this test support.
        # Check this test support.
        case "a pipe" if sys.platform != "win32":
            _clear(sabotaged)
            os.mkfifo(sabotaged)
        case "a socket" if sys.platform != "win32":
            _clear(sabotaged)
            with UNGUARDED_SOCKET(socket.AF_UNIX) as endpoint:
                endpoint.bind(str(sabotaged))
        case "nothing may use":
            if not sabotaged.is_dir():
                sabotaged.touch()
            sabotaged.chmod(0o000)
        case "nothing may write":
            if sabotaged.is_dir():
                sabotaged.chmod(0o555)
            else:
                sabotaged.touch()
                sabotaged.chmod(0o444)
        case _:
            raise AssertionError(shape)


class Exploding:
    @override
    def __str__(self) -> str:
        raise RuntimeError("boom")


class Turns:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def start(self, turn: int) -> None:
        (self.directory / str(turn)).touch()

    def wait_for(self, turn: int) -> None:
        _wait_for(self.directory / f"{turn}.done")


def _clear(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _grant_access(path: Path, wanted: int) -> None:
    with contextlib.suppress(OSError):
        path.chmod(path.stat().st_mode | wanted)


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not path.exists():
        assert time.monotonic() < deadline, f"the second run never reached {path.name}"
        time.sleep(POLL)
