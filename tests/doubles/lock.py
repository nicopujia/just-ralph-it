import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

CHILD_SUFFIX = ".child"
# A holder nothing killed would keep the lock for as long as the run
# lasts, so it gives up on its own well before a suite would.
HOLDER = """
import multiprocessing, sys, time
from pathlib import Path
from jri.lib.lock import Lock

def rest():
    time.sleep(30)

with Lock(Path(sys.argv[1])):
    if len(sys.argv) > 3:
        # `fork` is asked for by name, so a release that stops making
        # it the default cannot quietly retire what this holder is for.
        child = multiprocessing.get_context("fork").Process(target=rest)
        child.start()
        Path(sys.argv[3]).write_text(str(child.pid))
    Path(sys.argv[2]).touch()
    time.sleep(30)
"""
POLL = 0.01
TAKER = """
import sys
from pathlib import Path
from jri.lib.lock import Lock

with Lock(Path(sys.argv[1])):
    pass
"""
# A lock its holder's death freed is taken as soon as it is asked for,
# so this is only ever waited out by a lock that never came back.
TIMEOUT = 5


@contextmanager
def hold(path: Path, *, forking: bool = False) -> "Iterator[subprocess.Popen[bytes]]":
    ready = path.with_name(f"{path.name}.held")
    child = path.with_name(f"{path.name}{CHILD_SUFFIX}")
    command = [sys.executable, "-c", HOLDER, str(path), str(ready)]
    holder = subprocess.Popen([*command, str(child)] if forking else command)
    try:
        deadline = time.monotonic() + TIMEOUT
        while not ready.exists():
            assert time.monotonic() < deadline, "the holder never took the lock"
            time.sleep(POLL)
        yield holder
    finally:
        holder.kill()
        holder.wait()
        if child.exists():
            # A process the holder forked is nobody else's to reap, so
            # a suite that made one takes it down itself.
            os.kill(read_fork_child(path), signal.SIGTERM)


def read_fork_child(path: Path) -> int:
    return int(path.with_name(f"{path.name}{CHILD_SUFFIX}").read_text())


def runs(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def take(path: Path) -> bool:
    # The lock is asked for in a process of its own, so a lock that
    # never comes back ends the test rather than hanging the suite.
    try:
        taker = subprocess.run([sys.executable, "-c", TAKER, str(path)], timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return False
    return taker.returncode == 0
