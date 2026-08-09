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
#
# The ordinary closing of a terminal emulator never reaches here: that
# terminal is the process's controlling one, so the kernel sends SIGHUP
# to the foreground group and the default disposition ends the process
# in hundredths of a second, watch or no watch. This is for the cases
# the signal misses -- a pty that is nobody's controlling terminal, and
# an ignored SIGHUP inherited across exec from a parent like `nohup` --
# where a process with nowhere left to draw otherwise runs on for as
# long as its work lasts, holding everything it holds.
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
    # `select.poll` is not a thing Windows has: the class, `POLLHUP` and
    # `POLLERR` are all absent there, so this guard is what stands
    # between the watch and an `AttributeError` rather than belt over
    # braces. It is `select.select` that Windows answers for sockets
    # alone. Returning loses nothing anyway, since a console window
    # closing reaches its process as an event the system ends it on.
    if sys.platform == "win32":
        return
    descriptors = tuple(_list_descriptors())
    if not descriptors:
        return
    while not _has_hung_up(descriptors):
        time.sleep(POLL)
    end()
