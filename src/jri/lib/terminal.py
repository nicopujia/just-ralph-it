import select
import sys
import threading
import time
from collections.abc import Callable, Iterator
from typing import IO

__all__ = ["end_on_hangup"]

POLL = 0.25


# `end` is called once, in a thread of its own, as soon as the terminal
# this process was started in hangs up: the far side of it is closed,
# so nothing written there arrives and nothing read from it comes back.
# A process still drawing into one has no way left of saying so and no
# way of being asked to stop.
def end_on_hangup(end: Callable[[], None]) -> None:
    threading.Thread(target=_wait_for_hangup, args=(end,), daemon=True).start()


# What the operating system reports about the open file rather than
# about the bytes in it, so the answer costs nothing and takes no input
# away from whoever else is reading the terminal.
def _has_hung_up(descriptors: tuple[int, ...]) -> bool:
    poller = select.poll()
    for descriptor in descriptors:
        poller.register(descriptor, select.POLLHUP | select.POLLERR)
    return bool(poller.poll(0))


# The standard streams that are a terminal, which are the ones a hangup
# reaches. `sys.__stdin__` and its siblings rather than the current
# ones, since a stream something in this process put in their place is
# no longer the terminal's.
def _list_descriptors() -> Iterator[int]:
    streams: tuple[IO[str] | None, ...] = (sys.__stdin__, sys.__stdout__, sys.__stderr__)
    for stream in streams:
        try:
            if stream is not None and stream.isatty():
                yield stream.fileno()
        # A stream already closed, and one standing in for a terminal
        # without a descriptor of its own, are no terminal to watch.
        except (OSError, ValueError):
            continue


def _wait_for_hangup(end: Callable[[], None]) -> None:
    # A console window closing reaches its process as an event Windows
    # ends it on, and `poll` there answers about sockets alone.
    if sys.platform == "win32":
        return
    descriptors = tuple(_list_descriptors())
    if not descriptors:
        return
    while not _has_hung_up(descriptors):
        time.sleep(POLL)
    end()
