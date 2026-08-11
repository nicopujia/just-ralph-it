import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Self

# Each module exists only on its target platform. Import only the module
# for the current platform. Type checkers do not narrow by platform, so
# they import both modules and can check their calls.
if TYPE_CHECKING:
    import fcntl
    import msvcrt
elif sys.platform == "win32":
    import msvcrt
else:
    import fcntl

__all__ = ["Lock", "LockError"]

# `locking` locks a range of bytes. `flock` locks the complete file. Lock
# the first byte, then write holder data after that byte.
LOCKED_BYTES = 1
LOCK_FILE_PERMISSIONS = 0o600
# A JRI lock holder cannot write a longer record. This limit also stops a
# large lock file from causing a large read.
MAX_HOLDER_LENGTH = 64

# Store lock descriptors here, not in each lock. A child from `fork` can
# then close all inherited descriptors without access to their locks.
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

    # Return data from the lock holder, or an empty string if it wrote no
    # data. On Windows, a handle without the lock cannot read the locked
    # bytes. The record starts after those bytes, so it remains readable
    # while the lock is held.
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

    # The operating system releases a lock when its holder exits. A lock
    # that cannot be taken now has a running holder. This method releases
    # a lock that it takes, so the caller does not become its holder.
    def is_held(self) -> bool:
        if not self.take():
            return True
        self.release()
        return False

    def release(self) -> None:
        # A child from `fork` has no descriptor to release. It was removed
        # when the child closed its inherited descriptors.
        descriptor = _descriptors.pop(self, None)
        if descriptor is not None:
            _release(descriptor, taken=True)

    # Return whether the lock is free now. Do not wait for a held lock.
    # Write holder data while taking the lock. A reader cannot read data
    # from a holder that has already released the lock.
    def take(self, holder: str = "") -> bool:
        return self._acquire(holder, wait=False)

    def _acquire(self, holder: str, *, wait: bool) -> bool:
        try:
            # The file stores only the lock and its record. Do not open it
            # for writing, because that removes its data. Windows rejects
            # this action while another process locks these bytes.
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, LOCK_FILE_PERMISSIONS)
        except OSError as error:
            raise LockError(f"The lock file {self.path} cannot be opened.") from error
        # `fork` copies all open descriptors. Store this descriptor before
        # the wait, because a child can start during that wait.
        _descriptors[self] = descriptor
        taken = False
        try:
            if sys.platform == "win32":
                # `locking` starts at the current descriptor position. It
                # waits by trying again every tenth of a second. `flock`
                # waits without a limit on the number of tries.
                msvcrt.locking(descriptor, msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, LOCKED_BYTES)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
            taken = True
        except OSError as error:
            # A caller that does not wait expects false when it cannot
            # take the lock. All lock errors use the same result. Do not
            # report these errors as a lock held by this process.
            if wait:
                raise LockError(f"The lock over {self.path} cannot be taken.") from error
        finally:
            # Close a descriptor if it did not take the lock. Release only
            # a held lock later.
            if not taken:
                del _descriptors[self]
                os.close(descriptor)
        if not taken:
            return False
        if holder:
            record = holder.encode()
            os.lseek(descriptor, LOCKED_BYTES, os.SEEK_SET)
            written = os.write(descriptor, record)
            # A partial record can seem complete, but it can name another
            # holder. Old data after a partial record has the same problem.
            # End the file at the completed write only.
            os.ftruncate(descriptor, LOCKED_BYTES + (written if written == len(record) else 0))
        return True


def _drop_inherited() -> None:
    # The parent and child share the open file that owns the lock. If the
    # child unlocks it, it also releases the parent lock. Closing removes
    # only the child share of the open file.
    for descriptor in _descriptors.values():
        os.close(descriptor)
    _descriptors.clear()


# Closing releases a lock that the descriptor still holds. `locking` also
# requires an unlock for its locked range. Do both only after a lock is
# taken.
def _release(descriptor: int, *, taken: bool) -> None:
    if taken:
        if sys.platform == "win32":
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, LOCKED_BYTES)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


if sys.platform != "win32":
    # `os.open` returns a descriptor that `exec` closes. A process that
    # `subprocess` starts does not get it. `fork` copies it. The lock
    # remains while any copy of the open file is open. A child that runs
    # longer than its holder could otherwise keep the lock after exit.
    os.register_at_fork(after_in_child=_drop_inherited)
