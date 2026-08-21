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

# This is a window that holds the project and then releases it. The operating system releases the lock of a
# window that it ended in the same way. This window records a pid that the caller chooses, so a test can see
# which process an eviction signals while the project comes free.
BRIEF_HOLDER = """
import os, sys, time
from pathlib import Path
from jri.core import paths
from jri.lib.lock import Lock

root, ready, record, held_for = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], float(sys.argv[4])
assert Lock(root / paths.LOCK_FILE).take(record)
ready.write_text(str(os.getpid()) + "\\n")
time.sleep(held_for)
"""
# This is a process that never took the project. It replaces any process that gets the number of a holder after
# the operating system gives that number away. It writes a tick at each interval. A reader can then see a process
# that is alive, and not only a process that is not yet dead. A process cannot continue past a signal, because
# the kernel ends it before the next instruction it would run.
BYSTANDER = """
import os, sys, time
from pathlib import Path

beat = Path(sys.argv[3])
# The first tick comes before the pid, so a reader that holds the pid
# already has a tick to count from.
beat.write_bytes(b".")
Path(sys.argv[2]).write_text(str(os.getpid()) + "\\n")
while True:
    with beat.open("ab") as ticks:
        ticks.write(b".")
    time.sleep(0.005)
"""
# A holder stays alive while its test reads the project. A parallel run loads the machine. This time
# must be much longer than the slowest test. Each holder ends when its test ends.
HELD_FOR = 300
# This is a window that holds the project for the full test. `deaf` makes the window refuse each `SIGTERM` and
# write one byte for it. A test can then count how many times an eviction asked a window that does not answer
# to go.
HOLDER = f"""
import os, signal, sys, time
from pathlib import Path
from jri.core import paths
from jri.core.workspace import Workspace
from jri.lib.lock import Lock

root, ready, record = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
deaf, requests = sys.argv[4] == "deaf", Path(sys.argv[5])
if deaf:
    def turn_down(*_):
        with requests.open("ab") as asked:
            asked.write(b".")

    signal.signal(signal.SIGTERM, turn_down)
# A record of JRI's names the process that wrote it. A record of
# anything else names a holder that JRI cannot be.
taken = Lock(root / paths.LOCK_FILE).take(record) if record else Workspace(root).open_hold().take()
assert taken
ready.write_text(str(os.getpid()) + "\\n")
time.sleep({HELD_FOR})
"""
POLL = 0.01
# A signal reaches a sleeping window at once. Only a machine under load uses any of this time.
REQUESTS_WITHIN = 5.0
# This is a window that takes the lock and records its own pid later. It holds the claim across that delay. While
# the delay runs, the lock file still names the window before it. Only a reader that waits for the claim gets the
# pid of the window that holds the project now.
SLOW_HOLDER = f"""
import os, sys, time
from pathlib import Path
from jri.core import paths
from jri.lib.lock import LOCKED_BYTES, Lock

root, ready, delay = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
claim, lock = Lock(root / paths.CLAIM_FILE), Lock(root / paths.LOCK_FILE)
assert claim.take()
assert lock.take()
ready.write_text(str(os.getpid()) + "\\n")
time.sleep(delay)
descriptor = os.open(root / paths.LOCK_FILE, os.O_RDWR)
os.lseek(descriptor, LOCKED_BYTES, os.SEEK_SET)
written = os.write(descriptor, str(os.getpid()).encode())
# End the file at this pid, as `Lock` does. A shorter pid over a longer one leaves the digits of the window
# before it. The reader then reads a number that names no process.
os.ftruncate(descriptor, LOCKED_BYTES + written)
os.close(descriptor)
claim.release()
time.sleep({HELD_FOR})
"""
STARTS_WITHIN = 30
# The bystander writes two hundred ticks each second, so two ticks take one hundredth of a second. This time
# leaves room for a machine under load.
TICKS_WITHIN = 5.0


# This runs the two steps of the command, in the order of the command. It opens a reset first, and the install
# over an existing workspace then uses what the reset gives. `jri init --force` asks the user a question between
# the two steps. That question belongs to the window, and nothing here replaces it.
def install_workspace(path: Path, *, force: bool = False) -> Installation:
    workspace = Workspace(path)
    settings = Settings.render()
    if not force:
        return workspace.install(settings)
    with workspace.open_reset() as reset:
        return workspace.install(settings, reset=reset)


# This is a JRI that holds the project from a process of its own. A lock that the operating system frees says
# nothing about a holder inside this process.
@contextmanager
def hold_workspace(root: Path, *, record: str = "", deaf: bool = False) -> "Iterator[Process]":
    requests = _requests(root)
    requests.unlink(missing_ok=True)
    with _run(HOLDER, root, "held", (record, "deaf" if deaf else "", str(requests))) as holder:
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


# This tells whether the bystander still runs. It waits for two new ticks, and does not read the process one
# time. A process cannot write a tick after a signal reaches it. Two new ticks show that no signal reached it.
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


# This counts the requests to let the project go that reached a deaf window. It waits for the first request,
# because a loaded machine can delay a request that JRI did send.
def read_requests_to_go(root: Path) -> int:
    requests = _requests(root)
    deadline = time.monotonic() + REQUESTS_WITHIN
    while not (asked := requests.stat().st_size if requests.exists() else 0):
        assert time.monotonic() < deadline, "no request to let the project go reached the window"
        time.sleep(POLL)
    return asked


# This ends a window and waits for the project to come free. The operating system frees the lock of a process
# that it ended, and the window does not. Windows can need a moment for it. A read of the project at the instant
# the process dies reports a live window. `Hold.evict` waits for the same release, and does not read the lock
# one time.
def end_a_window(root: Path, window: "Process") -> None:
    window.kill()
    window.wait()
    deadline = time.monotonic() + Hold.FREED_WITHIN
    while Lock(root / paths.LOCK_FILE).is_held():
        assert time.monotonic() < deadline, "the killed window never let the project go"
        time.sleep(POLL)


# `Hold.evict` returns as soon as the project comes free. The operating system frees the lock of a window that it
# ended while that window still ends. A read of the process at that moment finds a window that still ends, and
# reports a window that holds the project. Wait for the full end, as `end_a_window` waits for the release.
def watch_a_window_go(window: "Process") -> bool:
    try:
        window.wait(Hold.FREED_WITHIN)
    except subprocess.TimeoutExpired:
        return False
    return True


@dataclass(frozen=True)
class Process:
    # A virtual environment on Windows starts the interpreter behind a launcher of its own. The number that a
    # spawn gives back then names that launcher, and not the process that runs the code. This pid is the number
    # that the process wrote about itself. The operating system keeps the lock for that process, and a signal to
    # the pid in the record ends it.
    pid: int
    spawn: subprocess.Popen[bytes]

    def kill(self) -> None:
        self.spawn.kill()

    def poll(self) -> int | None:
        return self.spawn.poll()

    def wait(self, timeout: float | None = None) -> int:
        return self.spawn.wait(timeout)


def _beat(root: Path) -> Path:
    # This stays outside the project, so a workspace holds only what the code under test put there.
    return root.parent / f"{root.name}.ticks"


def _requests(root: Path) -> Path:
    # This stays outside the project, so a workspace holds only what the code under test put there.
    return root.parent / f"{root.name}.requests"


def _read_pid(marker: Path) -> int:
    # The process writes its pid after it did the work that the test started it for. A marker that is still empty means
    # a process that never got there. The process makes the marker a moment before it writes to it. So this
    # waits for a number in the marker, and not for the file.
    # A write of a pid is not one step. A reader can find the first digits of it and read a number that names
    # another process. Each writer ends its pid with a line break, so a number without one still arrives.
    deadline = time.monotonic() + STARTS_WITHIN
    while not (pid := marker.read_text(encoding="utf-8") if marker.exists() else "").endswith("\n"):
        assert time.monotonic() < deadline, f"nothing wrote a pid to {marker}"
        time.sleep(POLL)
    return int(pid)


@contextmanager
def _run(source: str, root: Path, marker: str, arguments: "Sequence[str]") -> "Iterator[Process]":
    # This stays outside the project, so the window holds only what a test reads back out of it. The name uses the
    # role and not the root, because a test runs a window and a bystander over the same root.
    ready = root.parent / f"{root.name}.{marker}"
    # A process before this one in the same test left this marker. It belongs to the harness, and not to the code
    # under test.
    ready.unlink(missing_ok=True)
    spawn = subprocess.Popen([sys.executable, "-c", source, str(root), str(ready), *arguments])
    try:
        yield Process(_read_pid(ready), spawn)
    finally:
        spawn.kill()
        spawn.wait()
