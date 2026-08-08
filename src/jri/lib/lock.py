import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Self

# Neither module exists on the platform the other one is for, so only
# the platform's own is imported when this runs. A checker does not
# narrow by platform, so it is handed both and reads the calls into
# each of them instead of the `Any` a dynamic import would leave.
if TYPE_CHECKING:
    import fcntl
    import msvcrt
elif sys.platform == "win32":
    import msvcrt
else:
    import fcntl

__all__ = ["Lock", "LockError"]

# `locking` takes a range where `flock` takes the whole file, so the
# range is the first byte of the file, and what a holder writes about
# itself starts after it.
LOCKED_BYTES = 1
LOCK_FILE_PERMISSIONS = 0o600
# A record longer than this is not one a holder of JRI's wrote, and a
# read bounded here cannot be one the writer of a lock file grew.
MAX_HOLDER_LENGTH = 64

# The descriptor a lock is held on lives here rather than on the lock,
# so that a forked child can close every one it inherited without the
# locks that took them being reachable from it.
_descriptors: "dict[Lock, int]" = {}


class LockError(Exception): ...


class Lock:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> Self:
        self._acquire("", wait=True)
        return self

    def __exit__(self, *_: object) -> None:
        self.release()

    # What the holder of this lock wrote about itself, and nothing
    # where no holder wrote anything. The bytes the lock is taken over
    # are the operating system's on Windows -- a handle that did not
    # take the lock cannot read them -- so the record starts past them
    # and stays readable while the lock is held.
    @property
    def holder(self) -> str:
        try:
            descriptor = os.open(self.path, os.O_RDONLY)
        except OSError:
            return ""
        try:
            os.lseek(descriptor, LOCKED_BYTES, os.SEEK_SET)
            return os.read(descriptor, MAX_HOLDER_LENGTH).decode(errors="replace").strip()
        finally:
            os.close(descriptor)

    # The operating system drops the lock when its holder dies, so one
    # that cannot be taken this instant is one whose holder is running.
    # Asking is not taking: what this gets hold of it lets go of again,
    # so a caller learns the answer without becoming the holder.
    def is_held(self) -> bool:
        if not self.take():
            return True
        self.release()
        return False

    def release(self) -> None:
        # A block a forked child inherited has nothing left to release,
        # since the descriptor it was entered on is gone from the child.
        descriptor = _descriptors.pop(self, None)
        if descriptor is not None:
            _release(descriptor, taken=True)

    # Whether the lock was free this instant, without waiting for one
    # that was not. A holder names itself in the same breath, so no
    # reader of the record can be reading one a holder before it left.
    def take(self, holder: str = "") -> bool:
        return self._acquire(holder, wait=False)

    def _acquire(self, holder: str, *, wait: bool) -> bool:
        try:
            # The file is never anything but a handle to lock and a
            # record to read, and opening it for writing would truncate
            # it, which Windows refuses while another process holds a
            # lock over the bytes being dropped.
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, LOCK_FILE_PERMISSIONS)
        except OSError as error:
            raise LockError(f"The lock file {self.path} cannot be opened.") from error
        # A fork copies every descriptor the process has open, so this
        # one is written down before the wait for the range and not
        # after it: a child forked while the wait is on has it too.
        _descriptors[self] = descriptor
        taken = False
        try:
            if sys.platform == "win32":
                # `locking` covers bytes from wherever the descriptor
                # is, and waits by retrying ten times a second apart
                # rather than indefinitely as `flock` does.
                msvcrt.locking(descriptor, msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, LOCKED_BYTES)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
            taken = True
        except OSError as error:
            # A refusal is the answer a caller that would not wait
            # asked for, and every other reason a range cannot be
            # locked comes back the same way, so none of them is
            # reported as a lock this process has.
            if wait:
                raise LockError(f"The lock over {self.path} cannot be taken.") from error
        finally:
            # A descriptor no lock was taken on is a descriptor nothing
            # will close, since only a held lock is released.
            if not taken:
                del _descriptors[self]
                os.close(descriptor)
        if not taken:
            return False
        if holder:
            record = holder.encode()
            os.lseek(descriptor, LOCKED_BYTES, os.SEEK_SET)
            written = os.write(descriptor, record)
            # Part of a record reads as a whole one, and a name half
            # written is a name for something else. The tail of the
            # holder before this one reads the same way, so the file
            # ends where this write ended either way.
            os.ftruncate(descriptor, LOCKED_BYTES + (written if written == len(record) else 0))
        return True


def _drop_inherited() -> None:
    # The lock belongs to the open file the two processes now share, so
    # a child that unlocked would drop the lock its parent still holds.
    # Closing takes away only this process's share of it.
    for descriptor in _descriptors.values():
        os.close(descriptor)
    _descriptors.clear()


# Closing drops a lock the descriptor still holds, and `locking` leaves
# a range it was never given behind, so the two go together and only
# where a lock was taken.
def _release(descriptor: int, *, taken: bool) -> None:
    if taken:
        if sys.platform == "win32":
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, LOCKED_BYTES)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


if sys.platform != "win32":
    # `os.open` hands back a descriptor `exec` closes, so a process
    # `subprocess` starts never sees this one. `fork` copies it anyway,
    # and the lock stands for as long as any copy of the open file it
    # was taken on is open, so a forked child outliving the holder
    # would keep a lock the holder's death was supposed to drop.
    os.register_at_fork(after_in_child=_drop_inherited)
