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

# A process that never took the project, standing in for whatever wears
# a holder's number once the operating system has handed it on. It ticks
# so that a reader can tell being alive from not having died yet: a
# signal a process was sent is one it cannot outrun, since the kernel
# ends it before the next instruction it would have run.
BYSTANDER = """
import sys, time
from pathlib import Path

beat = Path(sys.argv[1])
while True:
    with beat.open("ab") as ticks:
        ticks.write(b".")
    time.sleep(0.005)
"""
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
# Two ticks at two hundred a second, with room for a machine under load.
TICKS_WITHIN = 5.0


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
def run_a_bystander(root: Path) -> "Iterator[subprocess.Popen[bytes]]":
    beat = _beat(root)
    beat.unlink(missing_ok=True)
    bystander = subprocess.Popen([sys.executable, "-c", BYSTANDER, str(beat)])
    try:
        deadline = time.monotonic() + STARTS_WITHIN
        while not beat.exists():
            assert time.monotonic() < deadline, "the bystander never started"
            time.sleep(POLL)
        yield bystander
    finally:
        bystander.kill()
        bystander.wait()


# Whether the bystander is still running, waited for rather than
# glanced at: a signal already sent is one it cannot tick past, so two
# ticks made after this was called are two the kernel would have ended
# it before.
def watch_a_bystander(root: Path, bystander: "subprocess.Popen[bytes]") -> bool:
    beat = _beat(root)
    ticks = beat.stat().st_size
    deadline = time.monotonic() + TICKS_WITHIN
    while beat.stat().st_size < ticks + 2:
        if bystander.poll() is not None:
            return False
        assert time.monotonic() < deadline, "the bystander stopped ticking"
        time.sleep(POLL)
    return True


def _beat(root: Path) -> Path:
    # Outside the project, so that what a workspace holds is only ever
    # what the code under test put there.
    return root.parent / f"{root.name}.ticks"


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
