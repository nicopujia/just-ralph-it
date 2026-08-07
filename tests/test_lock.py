import threading
import time
from pathlib import Path

from jri.lib.lock import Lock
from tests.doubles.lock import hold, take

HELD_FOR = 0.2


def test_refuses_a_lock_another_process_holds(tmp_path: Path) -> None:
    path = tmp_path / "lock"

    with hold(path):
        taken = Lock(path).acquire()

    assert not taken


def test_frees_the_lock_a_killed_holder_never_released(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    lock = Lock(path)

    with hold(path) as holder:
        holder.kill()
        holder.wait()
        taken = take(lock)

    lock.release()
    assert taken


def test_waits_for_the_lock_a_holder_has_not_dropped_yet(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    waiting = Lock(path)

    with hold(path) as holder:
        started = time.monotonic()
        # The holder is killed rather than asked to let go, so what the
        # wait ends on is the operating system and nothing of JRI's.
        threading.Timer(HELD_FOR, holder.kill).start()
        taken = waiting.acquire(wait=True)
        waited = time.monotonic() - started

    waiting.release()
    assert taken
    assert waited >= HELD_FOR


def test_refuses_a_lock_this_process_already_holds(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    held = Lock(path)
    held.acquire()

    taken = Lock(path).acquire()

    held.release()
    assert not taken


def test_holds_the_lock_for_the_block_it_was_taken_for(tmp_path: Path) -> None:
    path = tmp_path / "lock"

    with Lock(path):
        held = Lock.is_held(path)

    assert held
    assert not Lock.is_held(path)
