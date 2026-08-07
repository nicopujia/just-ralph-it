import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from jri.core import paths

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

POLL = 0.01
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


def list_log_files(workspace: Path) -> list[Path]:
    # Newest last, since the rotated file carries the older records.
    return sorted((workspace / paths.LOGS_DIR).glob(f"{Path(paths.LOG_FILE).name}*"), reverse=True)


def read_session_log(workspace: Path) -> str:
    return "".join(file.read_text(encoding="utf-8") for file in list_log_files(workspace))


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


class Turns:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def start(self, turn: int) -> None:
        (self.directory / str(turn)).touch()

    def wait_for(self, turn: int) -> None:
        _wait_for(self.directory / f"{turn}.done")


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not path.exists():
        assert time.monotonic() < deadline, f"the second run never reached {path.name}"
        time.sleep(POLL)
