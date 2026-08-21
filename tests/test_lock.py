import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from jri.lib.lock import Lock, LockError
from tests.doubles.lock import POLL, hold, read_fork_child, runs, take

# A killed holder frees its lock at once on POSIX. Only Windows uses any of this time.
FREED_WITHIN = 5.0
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
        # Kill the holder instead of asking it to release the lock.
        # The operating system, not JRI, ends the wait.
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
    # The child process outlives the holder in both cases.
    # An early wait means that the child released a lock it did not own.
    # A wait that does not end means that the child keeps the lock.
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


# The operating system frees the lock of a process that it ended, and the holder does not.
# Windows can need a short time to do it.
# Wait for the lock as `Hold.evict` does.
# A single read of the lock would find a holder that the kill already removed.
def test_reports_no_holder_for_a_lock_a_killed_holder_left(tmp_path: Path) -> None:
    path = tmp_path / "lock"

    with hold(path) as holder:
        holder.kill()
        holder.wait()

        deadline = time.monotonic() + FREED_WITHIN
        while Lock(path).is_held():
            assert time.monotonic() < deadline, "the lock of the killed holder never came free"
            time.sleep(POLL)


def test_reports_no_holder_for_a_lock_nothing_ever_took(tmp_path: Path) -> None:
    assert not Lock(tmp_path / "lock").is_held()
    # The check above does not take the lock, so this process can still take it.
    assert take(tmp_path / "lock")


def test_takes_a_lock_nothing_holds_without_waiting_for_it(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    lock = Lock(path)

    assert lock.take()

    assert not take(path)
    lock.release()
    assert take(path)


def test_refuses_a_lock_another_process_holds_rather_than_waiting(tmp_path: Path) -> None:
    path = tmp_path / "lock"

    with hold(path):
        started = time.monotonic()

        assert not Lock(path).take()

        assert time.monotonic() - started < HELD_FOR


def test_hands_back_the_record_the_holder_wrote(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    lock = Lock(path)

    assert lock.take("12345")

    # Read through an independent handle while this process holds the lock.
    # A second process reads the lock record in this way.
    assert Lock(path).holder == "12345"
    lock.release()


def test_replaces_the_record_the_holder_before_it_wrote(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    first = Lock(path)
    assert first.take("123456789")
    first.release()

    second = Lock(path)
    assert second.take("42")

    assert Lock(path).holder == "42"
    second.release()


def test_keeps_the_record_of_a_holder_that_let_the_lock_go(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    lock = Lock(path)
    assert lock.take("12345")

    lock.release()

    # The operating system frees the lock of a process that dies, and no code of that holder erases its record.
    # A release that erased the record would thus make a promise that a kill can always break.
    # The record stays, and a check of the lock keeps it.
    # A reader that wants the process that holds the lock now must find the lock held.
    # It must then read the record under an exclusion of its own.
    assert not Lock(path).is_held()
    assert Lock(path).holder == "12345"


def test_hands_back_no_record_from_a_write_that_stopped_halfway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "lock"
    first = Lock(path)
    assert first.take("123456789")
    first.release()
    write = os.write

    second = Lock(path)
    with monkeypatch.context() as halfway:
        halfway.setattr(os, "write", lambda descriptor, data: write(descriptor, data[: len(data) // 2]))
        assert second.take("4242")

    # A record that is cut short still reads as a number.
    # A reader would take that number for the holder, and end the process that has it.
    # No record at all tells the reader only that the holder wrote none.
    assert not Lock(path).holder
    second.release()


def test_hands_back_no_record_where_no_holder_wrote_one(tmp_path: Path) -> None:
    path = tmp_path / "lock"

    assert not Lock(path).holder

    with Lock(path):
        assert not Lock(path).holder


def test_reports_a_lock_file_it_cannot_open_to_answer_whether_it_is_held(tmp_path: Path) -> None:
    path = tmp_path / "directory"
    path.mkdir()

    with pytest.raises(LockError, match="cannot be opened"):
        Lock(path).is_held()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows keeps file access in a list `stat` does not report")
def test_keeps_a_lock_file_out_of_reach_of_the_other_users_of_the_machine(tmp_path: Path) -> None:
    path = tmp_path / "lock"

    with Lock(path):
        pass

    # Another user that can write this file can take the project from JRI.
    # That user can also name any process as the holder.
    # A person outside the project would then control the exclusion.
    assert not stat.S_IMODE(path.stat().st_mode) & 0o077
