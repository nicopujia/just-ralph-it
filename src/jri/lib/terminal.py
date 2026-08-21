import select
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import IO

__all__ = ["end_on_hangup"]

POLL = 0.25


# Call `end` one time in a separate thread when the terminal that started
# this process hangs up. The remote terminal has closed. Output cannot go
# there, and input cannot come from it. The process cannot report its
# state or receive a request to stop.
#
# This does not handle a usual terminal-emulator close. That terminal is
# the controlling terminal, so the kernel sends SIGHUP to the foreground
# group. Its default action stops the process quickly. This handles cases
# where SIGHUP does not stop it: a pty with no controlling terminal, or
# an ignored SIGHUP that `exec` inherits, for example from `nohup`.
# Without this check, such a process runs until its work ends and keeps
# its resources.
def end_on_hangup(end: Callable[[], None]) -> None:
    threading.Thread(target=_wait_for_hangup, args=(end,), daemon=True).start()


# Check the open file state that the operating system reports, not its
# bytes. This check does not use input from another terminal reader.
def _has_hung_up(descriptors: tuple[int, ...]) -> bool:
    poller = select.poll()
    for descriptor in descriptors:
        poller.register(descriptor, select.POLLHUP | select.POLLERR)
    return bool(poller.poll(0))


# List standard streams that are terminals, because a hangup reaches
# them. Use `sys.__stdin__` and related original streams. A stream that
# this process replaces is not the terminal stream.
def _list_descriptors() -> Iterator[int]:
    streams: tuple[IO[str] | None, ...] = (sys.__stdin__, sys.__stdout__, sys.__stderr__)
    for stream in streams:
        try:
            if stream is not None and stream.isatty():
                yield stream.fileno()
        # Do not watch a closed stream or a replacement stream without a
        # file descriptor.
        except (OSError, ValueError):
            continue


def _wait_for_hangup(end: Callable[[], None]) -> None:
    # Windows does not have `select.poll`, `POLLHUP`, or `POLLERR`. This
    # check prevents an `AttributeError`. Windows `select.select` works
    # only with sockets. A return here is safe, because a console-window
    # close sends an event that stops its process.
    if sys.platform == "win32":
        return
    descriptors = tuple(_list_descriptors())
    if not descriptors:
        return
    while not _has_hung_up(descriptors):
        time.sleep(POLL)
    end()
