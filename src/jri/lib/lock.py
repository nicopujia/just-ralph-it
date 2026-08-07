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
# range is the first byte of a file whose contents are never read.
LOCKED_BYTES = 1
LOCK_FILE_PERMISSIONS = 0o600

# The descriptor a lock is held on lives here rather than on the lock,
# so that a forked child can close every one it inherited without the
# locks that took them being reachable from it.
_descriptors: "dict[Lock, int]" = {}


class LockError(Exception): ...


class Lock:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> Self:
        try:
            # The file is never anything but a handle to lock, and
            # opening it for writing would truncate it, which Windows
            # refuses while another process holds a lock over the bytes
            # being dropped.
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
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, LOCKED_BYTES)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            taken = True
        except OSError as error:
            raise LockError(f"The lock over {self.path} cannot be taken.") from error
        finally:
            # A descriptor no lock was taken on is a descriptor nothing
            # will close, since only a held lock is released.
            if not taken:
                del _descriptors[self]
                os.close(descriptor)
        return self

    def __exit__(self, *_: object) -> None:
        # A block a forked child inherited has nothing left to release,
        # since the descriptor it was entered on is gone from the child.
        descriptor = _descriptors.pop(self, None)
        if descriptor is not None:
            if sys.platform == "win32":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, LOCKED_BYTES)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _drop_inherited() -> None:
    # The lock belongs to the open file the two processes now share, so
    # a child that unlocked would drop the lock its parent still holds.
    # Closing takes away only this process's share of it.
    for descriptor in _descriptors.values():
        os.close(descriptor)
    _descriptors.clear()


if sys.platform != "win32":
    # `os.open` hands back a descriptor `exec` closes, so a process
    # `subprocess` starts never sees this one. `fork` copies it anyway,
    # and the lock stands for as long as any copy of the open file it
    # was taken on is open, so a forked child outliving the holder
    # would keep a lock the holder's death was supposed to drop.
    os.register_at_fork(after_in_child=_drop_inherited)
