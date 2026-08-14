import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jri.core import paths
from jri.core.settings import Settings
from jri.core.workspace import Hold, Installation, Workspace
from jri.lib.lock import Lock

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

# This is a window that holds the project and then lets it go, as the operating system does for the lock of a
# window it ended. It records a pid of the caller's choosing, so a test can watch whom a takeover signals while
# the project is on its way to coming free.
BRIEF_HOLDER = """
import os, sys, time
from pathlib import Path
from jri.core import paths
from jri.lib.lock import Lock

root, ready, record, held_for = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], float(sys.argv[4])
assert Lock(root / paths.LOCK_FILE).take(record)
ready.write_text(str(os.getpid()))
time.sleep(held_for)
"""
# This is a process that never took the project. It stands for whatever wears the number of a holder after the
# operating system hands that number on. It ticks, thus a reader can see a process that is alive and not only a
# process that is not yet dead. A process cannot outrun a signal, because the kernel ends it before the next
# instruction it would run.
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
# A holder stays alive while its test reads the project. A parallel run loads the machine, thus this window
# must outlive the slowest test by a large margin. Each holder ends when its test ends.
HELD_FOR = 300
# This is a window that holds the project for the full test. `deaf` makes it ignore `SIGTERM`, thus a test can see
# what an eviction does against a window that does not answer.
HOLDER = f"""
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
time.sleep({HELD_FOR})
"""
POLL = 0.01
# This is a window that takes the lock and records its own pid later. It holds the claim across that delay. The lock
# file still names the window before it while the delay runs. Only a reader that waits for the claim gets the pid
# of the window that holds the project now.
SLOW_HOLDER = f"""
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
time.sleep({HELD_FOR})
"""
STARTS_WITHIN = 30
# Two ticks at two hundred each second, with room for a machine under load.
TICKS_WITHIN = 5.0


# These are the two steps of the command, in the order of the command: it opens a reset first, and the install over an
# existing workspace uses what the reset gives. `jri init --force` asks the user a question between the two steps.
# That question belongs to the window, and nothing here stands in for it.
def install_workspace(path: Path, *, force: bool = False) -> Installation:
    workspace = Workspace(path)
    settings = Settings.render()
    if not force:
        return workspace.install(settings)
    with workspace.open_reset() as reset:
        return workspace.install(settings, reset=reset)


# This is a JRI that holds the project from a process of its own. A lock that the operating system frees says nothing
# about a holder inside this process.
@contextmanager
def hold_workspace(root: Path, *, record: str = "", deaf: bool = False) -> "Iterator[Process]":
    with _run(HOLDER, root, "held", (record, "deaf" if deaf else "")) as holder:
        yield holder


@contextmanager
def hold_workspace_briefly(root: Path, held_for: float, *, record: str) -> "Iterator[Process]":
    with _run(BRIEF_HOLDER, root, "held", (record, str(held_for))) as holder:
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


# This tells whether the bystander still runs. It waits for two new ticks instead of one look at the process. A
# process cannot tick past a signal that it was sent. Two ticks after this call show that no signal reached it.
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


# End a window and wait for the project to come free. The operating system, not the window, frees the lock of a
# process it ended, and Windows can take a moment over it. A read of the project the instant the process dies
# calls that moment a live window. `Hold.evict` waits for the same release rather than reading the lock one time.
def end_a_window(root: Path, window: "Process") -> None:
    window.kill()
    window.wait()
    deadline = time.monotonic() + Hold.FREED_WITHIN
    while Lock(root / paths.LOCK_FILE).is_held():
        assert time.monotonic() < deadline, "the killed window never let the project go"
        time.sleep(POLL)


@dataclass(frozen=True)
class Process:
    # A virtual environment on Windows starts the interpreter behind a launcher of its own. The number a spawn gives
    # back then names that launcher and not the process that runs the code. This pid is the one the process wrote
    # about itself. The operating system keeps the lock for that process, and a signal out of the record would end it.
    pid: int
    spawn: subprocess.Popen[bytes]

    def kill(self) -> None:
        self.spawn.kill()

    def poll(self) -> int | None:
        return self.spawn.poll()

    def wait(self) -> int:
        return self.spawn.wait()


def _beat(root: Path) -> Path:
    # This stays outside the project, thus a workspace holds only what the code under test put there.
    return root.parent / f"{root.name}.ticks"


def _read_pid(marker: Path) -> int:
    # The pid lands after the process did what it was started to do. A marker that is still empty means a process
    # that never got there. A marker is made a moment before it is written, thus this waits for a number in it and not
    # for the file.
    deadline = time.monotonic() + STARTS_WITHIN
    while not (pid := marker.read_text(encoding="utf-8") if marker.exists() else "").isdigit():
        assert time.monotonic() < deadline, f"nothing wrote a pid to {marker}"
        time.sleep(POLL)
    return int(pid)


@contextmanager
def _run(source: str, root: Path, marker: str, arguments: "Sequence[str]") -> "Iterator[Process]":
    # This stays outside the project, thus the window holds only what a test reads back out of it. The name uses the
    # role and not the root, because a test runs a window and a bystander over the same root.
    ready = root.parent / f"{root.name}.{marker}"
    # A process before this one in the same test left this marker. It belongs to the harness and not to the code under
    # test.
    ready.unlink(missing_ok=True)
    spawn = subprocess.Popen([sys.executable, "-c", source, str(root), str(ready), *arguments])
    try:
        yield Process(_read_pid(ready), spawn)
    finally:
        spawn.kill()
        spawn.wait()
