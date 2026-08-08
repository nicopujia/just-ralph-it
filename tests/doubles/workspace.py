import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from jri.core.settings import Settings
from jri.core.workspace import Installation, Workspace

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

# A window nothing takes over would keep the project for as long as the
# run lasts, so it gives up on its own well before a suite would.
HOLDER = """
import signal, sys, time
from pathlib import Path
from jri.core import paths
from jri.core.workspace import Workspace
from jri.lib.lock import Lock

root, ready, record, deaf = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4] == "deaf"
if deaf:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
# A record of JRI's names the process that wrote it, and one of
# anything else stands for a holder JRI has no way of being.
taken = Lock(root / paths.LOCK_FILE).take(record) if record else Workspace(root).open_hold().take()
assert taken
ready.touch()
time.sleep(30)
"""
POLL = 0.01
# A window caught between taking the lock and writing down which
# process took it: for that moment the record on disk is the one the
# window before it left, and the lock is already this one's.
SLOW_HOLDER = """
import os, sys, time
from pathlib import Path
from jri.core import paths
from jri.lib.lock import LOCKED_BYTES, Lock

root, ready, delay = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
claim, lock = Lock(root / paths.CLAIM_FILE), Lock(root / paths.LOCK_FILE)
assert claim.take()
assert lock.take()
ready.touch()
time.sleep(delay)
descriptor = os.open(root / paths.LOCK_FILE, os.O_RDWR)
os.lseek(descriptor, LOCKED_BYTES, os.SEEK_SET)
os.write(descriptor, str(os.getpid()).encode())
os.close(descriptor)
claim.release()
time.sleep(30)
"""
STARTS_WITHIN = 30


def install_workspace(path: Path, *, force: bool = False) -> Installation:
    return Workspace(path).install(Settings.render_config(), force=force)


# A JRI holding the project from a process of its own, since a lock the
# operating system frees says nothing about a holder inside this one.
@contextmanager
def hold_workspace(root: Path, *, record: str = "", deaf: bool = False) -> "Iterator[subprocess.Popen[bytes]]":
    with _run(HOLDER, root, (record, "deaf" if deaf else "")) as holder:
        yield holder


@contextmanager
def hold_workspace_slowly(root: Path, delay: float) -> "Iterator[subprocess.Popen[bytes]]":
    with _run(SLOW_HOLDER, root, (str(delay),)) as holder:
        yield holder


@contextmanager
def _run(source: str, root: Path, arguments: "Sequence[str]") -> "Iterator[subprocess.Popen[bytes]]":
    # Outside the project, so that what the window holds is the only
    # thing a test reads back out of it.
    ready = root.parent / f"{root.name}.held"
    # The marker a window before this one in the same test left, which
    # is the harness's own and nothing of the code under test.
    ready.unlink(missing_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", source, str(root), str(ready), *arguments])
    try:
        deadline = time.monotonic() + STARTS_WITHIN
        while not ready.exists():
            assert time.monotonic() < deadline, "the window never took the project"
            time.sleep(POLL)
        yield holder
    finally:
        holder.kill()
        holder.wait()
