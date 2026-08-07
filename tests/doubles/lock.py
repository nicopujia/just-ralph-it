import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from jri.lib.lock import Lock

if TYPE_CHECKING:
    from collections.abc import Iterator

# A holder nothing killed would keep the lock for as long as the run
# lasts, so it gives up on its own well before a suite would.
HOLDER = """
import sys, time
from pathlib import Path
from jri.lib.lock import Lock
lock = Lock(Path(sys.argv[1]))
lock.acquire(wait=True)
Path(sys.argv[2]).touch()
time.sleep(30)
"""
POLL = 0.01
TIMEOUT = 10


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


def take(lock: Lock) -> bool:
    # Windows hands a dead holder's range back when it gets round to
    # it, so the wait is the platform's rather than the lock's.
    deadline = time.monotonic() + TIMEOUT
    while not lock.acquire():
        if time.monotonic() > deadline:
            return False
        time.sleep(POLL)
    return True
