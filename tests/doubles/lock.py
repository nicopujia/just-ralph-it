import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

CHILD_SUFFIX = ".child"
# A holder stays alive while its test reads the lock. A parallel run loads the machine, thus this window
# must outlive the slowest test by a large margin. `hold` ends its holder when the test ends.
HELD_FOR = 300
HOLDER = f"""
import multiprocessing, os, sys, time
from pathlib import Path
from jri.lib.lock import Lock

def rest():
    time.sleep({HELD_FOR})

path, ready, record, child = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
assert Lock(path).take(str(os.getpid()) if record == "own" else record)
if child:
    # `fork` is asked for by name, so a release that stops making
    # it the default cannot quietly retire what this holder is for.
    started = multiprocessing.get_context("fork").Process(target=rest)
    started.start()
    Path(child).write_text(str(started.pid))
ready.touch()
time.sleep({HELD_FOR})
"""
# This asks a holder for the record a runner writes: its own pid. Only the holder knows that number.
OWN_PID = "own"
POLL = 0.01
TAKER = """
import sys
from pathlib import Path
from jri.lib.lock import Lock

with Lock(Path(sys.argv[1])):
    pass
"""
# The death of a holder frees the lock, and the next taker gets it immediately. Only a lock that stays
# held uses all of this time.
TIMEOUT = 5


# This is a process that holds a lock for the whole test. It writes the record the caller asks for, and nothing
# when the caller asks for none. `session` gives it a session of its own, thus its process group holds only what it
# starts, and `forking` gives it one such process.
@contextmanager
def hold(
    path: Path, *, record: str = "", forking: bool = False, session: bool = False
) -> "Iterator[subprocess.Popen[bytes]]":
    ready = path.with_name(f"{path.name}.held")
    child = path.with_name(f"{path.name}{CHILD_SUFFIX}")
    # A holder before this one in the same test left these markers. They belong to the harness and not to the code
    # under test.
    ready.unlink(missing_ok=True)
    child.unlink(missing_ok=True)
    command = [sys.executable, "-c", HOLDER, str(path), str(ready), record, str(child) if forking else ""]
    holder = subprocess.Popen(command, start_new_session=session)
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
            # No other process reaps a process that the holder forked. The test that made it must stop it.
            # A test that ended this process already leaves nothing here to stop.
            with suppress(OSError):
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
    # A separate process asks for the lock. A lock that never comes back becomes a failed result here.
    # The suite does not stop.
    try:
        taker = subprocess.run([sys.executable, "-c", TAKER, str(path)], timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return False
    return taker.returncode == 0


# The end of a process is not one step: the operating system ends it, and its parent then reaps it. A reader that
# looks one time can meet a process on its way out. Wait for the end itself.
def watch_a_process_go(pid: int) -> bool:
    deadline = time.monotonic() + TIMEOUT
    while runs(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL)
    return True
