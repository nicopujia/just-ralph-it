import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# A holder nothing killed would keep the lock for as long as the run
# lasts, so it gives up on its own well before a suite would.
HOLDER = """
import sys, time
from pathlib import Path
from jri.lib.lock import Lock

with Lock(Path(sys.argv[1])):
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
def hold(path: Path) -> "Iterator[subprocess.Popen[bytes]]":
    ready = path.with_name(f"{path.name}.held")
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(path), str(ready)])
    try:
        deadline = time.monotonic() + TIMEOUT
        while not ready.exists():
            assert time.monotonic() < deadline, "the holder never took the lock"
            time.sleep(POLL)
        yield holder
    finally:
        holder.kill()
        holder.wait()


def take(path: Path) -> bool:
    # The lock is asked for in a process of its own, so a lock that
    # never comes back ends the test rather than hanging the suite.
    try:
        taker = subprocess.run([sys.executable, "-c", TAKER, str(path)], timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return False
    return taker.returncode == 0
