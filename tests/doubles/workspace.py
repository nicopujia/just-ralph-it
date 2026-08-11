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

# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
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
# Check this test support.
# Check this test support.
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
# Check this test support.
# Check this test support.
# Check this test support.
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
# Check this test support.
TICKS_WITHIN = 5.0


# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
def install_workspace(path: Path, *, force: bool = False) -> Installation:
    workspace = Workspace(path)
    config = Settings.render_config()
    if not force:
        return workspace.install(config)
    with workspace.open_reset() as reset:
        return workspace.install(config, reset=reset)


# Check this test support.
# Check this test support.
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


# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
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
    # Check this test support.
    # Check this test support.
    # Check this test support.
    # Check this test support.
    # Check this test support.
    # Check this test support.
    pid: int
    spawn: subprocess.Popen[bytes]

    def kill(self) -> None:
        self.spawn.kill()

    def poll(self) -> int | None:
        return self.spawn.poll()

    def wait(self) -> int:
        return self.spawn.wait()


def _beat(root: Path) -> Path:
    # Check this test support.
    # Check this test support.
    return root.parent / f"{root.name}.ticks"


def _read_pid(marker: Path) -> int:
    # Check this test support.
    # Check this test support.
    # Check this test support.
    # Check this test support.
    deadline = time.monotonic() + STARTS_WITHIN
    while not (pid := marker.read_text(encoding="utf-8") if marker.exists() else "").isdigit():
        assert time.monotonic() < deadline, f"nothing wrote a pid to {marker}"
        time.sleep(POLL)
    return int(pid)


@contextmanager
def _run(source: str, root: Path, marker: str, arguments: "Sequence[str]") -> "Iterator[Process]":
    # Check this test support.
    # Check this test support.
    # Check this test support.
    # Check this test support.
    ready = root.parent / f"{root.name}.{marker}"
    # Check this test support.
    # Check this test support.
    ready.unlink(missing_ok=True)
    spawn = subprocess.Popen([sys.executable, "-c", source, str(root), str(ready), *arguments])
    try:
        yield Process(_read_pid(ready), spawn)
    finally:
        spawn.kill()
        spawn.wait()
