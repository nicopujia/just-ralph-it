import sys
import threading
import time
from pathlib import Path

import pytest

from jri.lib.lock import Lock, LockError
from tests.doubles.lock import hold, read_fork_child, runs, take

HELD_FOR = 0.5


def test_frees_the_lock_when_the_block_it_was_taken_for_ends(tmp_path: Path) -> None:
    path = tmp_path / "lock"

    with Lock(path):
        pass

    assert take(path)


def test_waits_for_the_lock_a_holder_has_not_dropped_yet(tmp_path: Path) -> None:
    path = tmp_path / "lock"

    with hold(path) as holder:
        started = time.monotonic()
        # The holder is killed rather than asked to let go, so what the
        # wait ends on is the operating system and nothing of JRI's.
        threading.Timer(HELD_FOR, holder.kill).start()
        taken = take(path)
        waited = time.monotonic() - started

    assert taken
    assert waited >= HELD_FOR


@pytest.mark.skipif(sys.platform == "win32", reason="`fork` is the one way of copying a descriptor Windows has not")
def test_frees_the_lock_a_killed_holder_left_to_a_process_it_forked(tmp_path: Path) -> None:
    path = tmp_path / "lock"

    with hold(path, forking=True) as holder:
        started = time.monotonic()
        threading.Timer(HELD_FOR, holder.kill).start()
        taken = take(path)
        waited = time.monotonic() - started
        inherited = runs(read_fork_child(path))

    assert inherited, "the forked process died before it could hold on to the lock"
    assert taken
    # The forked process outlives the holder either way, so a wait that
    # ended early is the child having dropped a lock that was not its
    # own to drop, and a wait that never ends is the child keeping one.
    assert waited >= HELD_FOR


@pytest.mark.skipif(sys.platform == "win32", reason="a directory that refuses a write is an access list `chmod` cannot")
def test_reports_a_lock_file_it_cannot_open(tmp_path: Path) -> None:
    unwritable = tmp_path / "unwritable"
    unwritable.mkdir(mode=0o500)

    try:
        with pytest.raises(LockError, match="cannot be opened"), Lock(unwritable / "lock"):
            pass
    finally:
        unwritable.chmod(0o700)


def test_reports_a_lock_path_that_is_not_a_file(tmp_path: Path) -> None:
    path = tmp_path / "directory"
    path.mkdir()

    with pytest.raises(LockError, match="cannot be opened"), Lock(path):
        pass

    assert path.is_dir()


def test_reports_the_lock_a_holder_still_running_has(tmp_path: Path) -> None:
    path = tmp_path / "lock"

    with hold(path):
        assert Lock(path).is_held()


def test_reports_no_holder_for_a_lock_a_killed_holder_left(tmp_path: Path) -> None:
    path = tmp_path / "lock"

    with hold(path) as holder:
        holder.kill()
        holder.wait()

        assert not Lock(path).is_held()


def test_reports_no_holder_for_a_lock_nothing_ever_took(tmp_path: Path) -> None:
    assert not Lock(tmp_path / "lock").is_held()
    # Asking is not taking, so the lock is still there to be had.
    assert take(tmp_path / "lock")


def test_reports_a_lock_file_it_cannot_open_to_answer_who_holds_it(tmp_path: Path) -> None:
    path = tmp_path / "directory"
    path.mkdir()

    with pytest.raises(LockError, match="cannot be opened"):
        Lock(path).is_held()
