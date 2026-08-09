import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
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
import os, sys, time
from pathlib import Path

beat = Path(sys.argv[3])
# The first tick lands before the pid does, so a reader holding the pid
# has a beat to count on from the moment it has it.
beat.write_bytes(b".")
Path(sys.argv[2]).write_text(str(os.getpid()))
while True:
    with beat.open("ab") as ticks:
        ticks.write(b".")
    time.sleep(0.005)
"""
# A window nothing takes over would keep the project for as long as the
# run lasts, so it gives up on its own well before a suite would.
HOLDER = """
import os, signal, sys, time
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
ready.write_text(str(os.getpid()))
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
ready.write_text(str(os.getpid()))
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


# The command's own two steps, in its own order: a reset is opened
# first, and what installing over an existing workspace takes is what
# that hands back. The question `jri init --force` asks between the two
# is the window's, and nothing here stands in for it.
def install_workspace(path: Path, *, force: bool = False) -> Installation:
    workspace = Workspace(path)
    config = Settings.render_config()
    if not force:
        return workspace.install(config)
    with workspace.open_reset() as reset:
        return workspace.install(config, reset=reset)


# A JRI holding the project from a process of its own, since a lock the
# operating system frees says nothing about a holder inside this one.
@contextmanager
def hold_workspace(root: Path, *, record: str = "", deaf: bool = False) -> "Iterator[Process]":
    with _run(HOLDER, root, "held", (record, "deaf" if deaf else "")) as holder:
        yield holder


@contextmanager
def hold_workspace_slowly(root: Path, delay: float) -> "Iterator[Process]":
    with _run(SLOW_HOLDER, root, "held", (str(delay),)) as holder:
        yield holder


@contextmanager
def run_a_bystander(root: Path) -> "Iterator[Process]":
    beat = _beat(root)
    beat.unlink(missing_ok=True)
    with _run(BYSTANDER, root, "ticking", (str(beat),)) as bystander:
        yield bystander


# Whether the bystander is still running, waited for rather than
# glanced at: a signal already sent is one it cannot tick past, so two
# ticks made after this was called are two the kernel would have ended
# it before.
def watch_a_bystander(root: Path, bystander: "Process") -> bool:
    beat = _beat(root)
    ticks = beat.stat().st_size
    deadline = time.monotonic() + TICKS_WITHIN
    while beat.stat().st_size < ticks + 2:
        if bystander.poll() is not None:
            return False
        assert time.monotonic() < deadline, "the bystander stopped ticking"
        time.sleep(POLL)
    return True


@dataclass(frozen=True)
class Process:
    # A virtual environment starts the interpreter behind a launcher of
    # its own on Windows, so the number a spawn hands back names that
    # launcher rather than the process running the code. The pid a test
    # holds a lock's record against is the one the process wrote down
    # about itself: that is the process the operating system keeps the
    # lock for, and the one a signal out of the record would end.
    pid: int
    spawn: subprocess.Popen[bytes]

    def kill(self) -> None:
        self.spawn.kill()

    def poll(self) -> int | None:
        return self.spawn.poll()

    def wait(self) -> int:
        return self.spawn.wait()


def _beat(root: Path) -> Path:
    # Outside the project, so that what a workspace holds is only ever
    # what the code under test put there.
    return root.parent / f"{root.name}.ticks"


def _read_pid(marker: Path) -> int:
    # The pid lands once the process has done what it was started to
    # do, so a marker still empty is a process that never got there --
    # and a marker is made a moment before it is written, which is why
    # what is waited for is a number in it and not the file being there.
    deadline = time.monotonic() + STARTS_WITHIN
    while not (pid := marker.read_text(encoding="utf-8") if marker.exists() else "").isdigit():
        assert time.monotonic() < deadline, f"nothing wrote a pid to {marker}"
        time.sleep(POLL)
    return int(pid)


@contextmanager
def _run(source: str, root: Path, marker: str, arguments: "Sequence[str]") -> "Iterator[Process]":
    # Outside the project, so that what the window holds is the only
    # thing a test reads back out of it. Named for the role rather than
    # the root, since a test runs a window and a bystander over the
    # same one.
    ready = root.parent / f"{root.name}.{marker}"
    # The marker a process before this one in the same test left, which
    # is the harness's own and nothing of the code under test.
    ready.unlink(missing_ok=True)
    spawn = subprocess.Popen([sys.executable, "-c", source, str(root), str(ready), *arguments])
    try:
        yield Process(_read_pid(ready), spawn)
    finally:
        spawn.kill()
        spawn.wait()
