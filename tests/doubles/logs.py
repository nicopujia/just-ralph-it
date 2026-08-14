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

# `jri.core.logs` makes and repairs these three paths. A test leaves each of `SABOTAGE_SHAPES` on each one.
LOG_PATHS = (paths.LOGS_DIR, paths.LOG_LOCK_FILE, paths.LOG_FILE)
POLL = 0.01
# These are the states an unprivileged process can leave on one of those names. `sabotage` makes each state on each
# path. Three more states are absent for a reason, not for want of looking. An immutable or append-only file needs
# `CAP_LINUX_IMMUTABLE`, and a device node needs `CAP_MKNOD`. This process has neither privilege. A filesystem with no
# space left needs a mount of its own, or it fills a mount the whole machine shares, and no repair puts back a record
# that is lost for want of a block. A test that takes the `.jri` name covers what the run does when a write can never
# land. Two other states are no state of their own. A name that collides on a case-insensitive filesystem is `a file`
# or `a directory` there, because `JRI.LOG` is `jri.log`. A path longer than `PATH_MAX` needs a project root that the
# user chose, because `NAME_MAX` stops each component here from growing.
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
    # Python cannot make these two on Windows. No pipe of that platform answers to a path, and `socket` there has no
    # `AF_UNIX`. The platform withholds these two shapes, not a privilege.
    *(() if sys.platform == "win32" else ("a pipe", "a socket")),
)
# This is a second run of the same session. It does what `jri view` does beside a `jri chat`: it configures the same
# log and writes to it. It waits for its turn between batches, thus a test can put its own records before, after, or
# beside the records of this run.
SECOND_RUN = """
import logging, sys, time
from pathlib import Path
from types import SimpleNamespace

from jri.core import logs

bound, turns = int(sys.argv[1]), Path(sys.argv[2])
padding = Path(sys.argv[4]).read_text(encoding="utf-8")
handoff = 0.0005
logs.LOG_FILE_BYTES = bound
logs.configure(SimpleNamespace(logging=SimpleNamespace(level="INFO")))
logger = logging.getLogger("jri.view")
(turns / "configured").touch()
for turn, size in enumerate(int(batch) for batch in sys.argv[3].split(",")):
    while not (turns / str(turn)).exists():
        time.sleep(handoff)
    for index in range(size):
        logger.info("VIEW %d %d %s", turn, index, padding)
    (turns / f"{turn}.done").touch()
logging.shutdown()
"""
# A turn that never comes stops the test. It does not hang the suite.
TIMEOUT = 30
# `tests.conftest.isolate_network` puts a guard on `socket.socket`. A socket bound to a name under `.jri` reaches
# nothing, thus this keeps the constructor at import time, before the guard stands. The shape below is then made and
# not skipped.
UNGUARDED_SOCKET = socket.socket
# This is a directory of the user beside `.jri`. It holds what the links and the hard links below point at. A record
# that reaches something below it is a record that left the workspace directory.
USER_FILES_DIR = "src"


# A sabotage can leave any shape on a log name. Such a name holds no record, and a read of a pipe never comes back.
# This lists the regular files only, and a name that still stands wrong reads as a record that is gone.
# The pattern is wider than the one log path, because a session must leave this file and no file beside it. A sort
# makes the list the same on each machine, because a directory gives its names in the order it holds them.
def list_log_files(workspace: Path) -> list[Path]:
    return sorted(
        file
        for file in (workspace / paths.LOGS_DIR).glob(f"{Path(paths.LOG_FILE).name}*")
        if not file.is_symlink() and file.is_file()
    )


# A mode that a sabotage left is the mode of the sabotage and not the mode of the log. The test owns these files, thus
# this takes the access back. It changes nothing the log wrote: a record the run never managed to write is missing
# either way.
def read_session_log(workspace: Path) -> str:
    _grant_access(workspace / paths.LOGS_DIR, stat.S_IRWXU)
    files = list_log_files(workspace)
    for file in files:
        _grant_access(file, stat.S_IRUSR)
    return "".join(file.read_text(encoding="utf-8") for file in files)


# This gives everything the project holds beside `.jri`, with the byte length of each. A record that reached one of
# these made it longer. A record that made a file of its own left a name that was not here before. This measures a
# link and does not follow it, and it does not walk the directory that a link stands for. It counts the project
# and never a tree that the log was pointed at from inside `.jri`.
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
    # An argument is limited to `MAX_ARG_STRLEN`, which is 128 KiB on Linux. These runs must write a record as large
    # as the log bound. The payload reaches the second run in a file and not in one of its arguments.
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
    # The terminal belongs to a `jri chat` screen. A run that logged has nothing to say on it, and `logging` says much
    # when it fails.
    assert not reported, f"the second run wrote this to the terminal:\n{reported}"
    assert run.returncode == 0


# This leaves one of `SABOTAGE_SHAPES` on one of `LOG_PATHS`. A link points into the files of the user and not at
# nothing in particular, because a link that the log follows out of `.jri` is the risk to catch. The two dangling
# links differ by whether the name they point at can be created. Only `a link one write away` lets an `O_CREAT` land
# outside.
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
        # These guards say what the shapes above already say. They let a checker read these two arms on a platform
        # whose `os` and whose `socket` do not have what the arms call.
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
