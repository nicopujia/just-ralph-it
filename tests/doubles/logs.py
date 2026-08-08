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

from jri.core import logs, paths

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

# Every path the log needs: the directory holding them, the lock the
# runs of a session take turns over, and each name the rotation walks
# through, which follows `KEPT_LOG_FILES` rather than restating it.
# Their parents are not here. `.jri` holds the notebook, the
# configuration and the specifications, so it is not the log's to
# clear the way it clears the names under it, and what a file standing
# on it costs the run has a test of its own. The project root is what
# every file JRI writes shares, so a wrong one there is not the log's
# class at all.
LOG_PATHS = (
    paths.LOGS_DIR,
    paths.LOG_LOCK_FILE,
    paths.LOG_FILE,
    *(f"{paths.LOG_FILE}.{index}" for index in range(1, logs.KEPT_LOG_FILES)),
)
POLL = 0.01
# What an unprivileged process can leave on one of those names, and
# `sabotage` makes every one of them on every one of those paths.
# Three states are left out, each for a reason rather than for want of
# looking. An immutable or append-only file needs
# `CAP_LINUX_IMMUTABLE` and a device node needs `CAP_MKNOD`, so
# neither is this process's to make. A filesystem with nothing left on
# it needs a mount of one's own or the filling of one the whole
# machine shares, and a record dropped for want of a block is one no
# repair puts back -- what the run does when a write can never land is
# pinned where `.jri` is taken instead. Two more are no state of their
# own: a name that collides on a case-insensitive filesystem is `a
# file` or `a directory` there, since `JRI.LOG` *is* `jri.log`, and a
# path past `PATH_MAX` needs a project root the user chose, since
# `NAME_MAX` stops any component here from growing.
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
    # Neither is Python's to make on Windows: no pipe of the
    # platform's own answers to a path, and `socket` there carries no
    # `AF_UNIX`. The platform withholds these two, not a privilege.
    *(() if sys.platform == "win32" else ("a pipe", "a socket")),
)
# A second run of the same session, doing what `jri view` does beside a
# `jri chat`: configuring the same log and writing to it. It waits for
# its turn between batches, so a test can put its own records before,
# after or alongside this run's.
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
# A turn that never comes ends the test rather than hanging the suite.
TIMEOUT = 30
# `tests.conftest.isolate_network` stands a guard on `socket.socket`,
# and a socket bound to a name under `.jri` reaches nothing at all, so
# the constructor is taken here -- at import, before the guard stands
# -- and the shape below is made rather than skipped.
UNGUARDED_SOCKET = socket.socket
# A directory of the user's beside `.jri`, holding what the links and
# the hard links below point at. A record that reaches anything under
# it is a record that left the workspace directory.
USER_FILES_DIR = "src"


# Newest last, since the rotated file carries the older records. A
# name the log rotates through that holds something else holds no
# record either, and a pipe on one of them answers a read by never
# coming back, so what is listed is the regular files and a name still
# standing wrong reads as the record it was holding gone.
def list_log_files(workspace: Path) -> list[Path]:
    return sorted(
        (
            file
            for file in (workspace / paths.LOGS_DIR).glob(f"{Path(paths.LOG_FILE).name}*")
            if not file.is_symlink() and file.is_file()
        ),
        reverse=True,
    )


# A mode a sabotage left behind is the sabotage's and not the log's,
# and whoever reads a session's records back owns the files, so the
# access is taken back here. What the log wrote is unchanged by it:
# a record the run never managed to write is missing either way.
def read_session_log(workspace: Path) -> str:
    _grant_access(workspace / paths.LOGS_DIR, stat.S_IRWXU)
    files = list_log_files(workspace)
    for file in files:
        _grant_access(file, stat.S_IRUSR)
    return "".join(file.read_text(encoding="utf-8") for file in files)


# Everything the project holds beside `.jri`, by the bytes it is long:
# a record that reached one of these grew it, and one that made a file
# of its own left a name that was not here before. A link is measured
# rather than followed, and a directory a link stands for is not
# walked, so what is counted is the project and never a tree the log
# was pointed at from inside `.jri`.
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
    # An argument is capped at `MAX_ARG_STRLEN` -- 128 KiB on Linux --
    # and a record the size the log bounds is what these runs are for,
    # so the payload reaches the second run as a file rather than as
    # one of its arguments.
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
    # The terminal is a `jri chat` screen's, so a run that logged has
    # nothing to say on it -- and `logging` says plenty when it fails.
    assert not reported, f"the second run wrote this to the terminal:\n{reported}"
    assert run.returncode == 0


# One of `SABOTAGE_SHAPES` left on one of `LOG_PATHS`. A link goes
# somewhere the user's rather than nowhere in particular, since a link
# the log follows out of `.jri` is the thing worth catching, and the
# two dangling ones are told apart by whether the name they point at
# can be created: only the second one lets an `O_CREAT` land outside.
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
        # The guards say what the shapes above already say, and are
        # what lets a checker read these two arms on a platform whose
        # `os` and whose `socket` do not carry what they call.
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
