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


class LockError(Exception): ...


class Lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._descriptor: int | None = None

    def __enter__(self) -> Self:
        try:
            # The file is never anything but a handle to lock, and
            # opening it for writing would truncate it, which Windows
            # refuses while another process holds a lock over the bytes
            # being dropped.
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, LOCK_FILE_PERMISSIONS)
        except OSError as error:
            raise LockError(f"The lock file {self.path} cannot be opened.") from error
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
                os.close(descriptor)
        self._descriptor = descriptor
        return self

    def __exit__(self, *_: object) -> None:
        if self._descriptor is not None:
            if sys.platform == "win32":
                os.lseek(self._descriptor, 0, os.SEEK_SET)
                msvcrt.locking(self._descriptor, msvcrt.LK_UNLCK, LOCKED_BYTES)
            else:
                fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            os.close(self._descriptor)
            self._descriptor = None
