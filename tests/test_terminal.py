import os
import sys
import threading

import pytest

from jri.lib.terminal import end_on_hangup

ANSWERS_WITHIN = 5.0
# This duration lets the watcher poll more than one time.
STANDS_FOR = 1.0


@pytest.mark.skipif(sys.platform == "win32", reason="a pseudo-terminal is the one terminal a test can hang up")
def test_answers_the_hangup_of_the_terminal_the_process_was_started_in(monkeypatch: pytest.MonkeyPatch) -> None:
    # Use `os.openpty` instead of `pty.openpty`.
    # Importing `pty` imports `termios`, which Windows does not provide.
    # An import failure occurs before the skip can handle the platform.
    # This pseudo-terminal is not the controlling terminal of a process.
    # This is the case that the watcher must handle.
    # A controlling terminal would end the process on `SIGHUP` first.
    # Then the test could not distinguish the two conditions.
    master, slave = os.openpty()
    with os.fdopen(slave, "rb", buffering=0) as terminal:
        for stream in ("__stdin__", "__stdout__", "__stderr__"):
            monkeypatch.setattr(sys, stream, terminal)
        ended = threading.Event()

        end_on_hangup(ended.set)

        assert not ended.wait(STANDS_FOR), "a terminal that is still there is not a hangup"
        os.close(master)
        assert ended.wait(ANSWERS_WITHIN)


@pytest.mark.skipif(sys.platform == "win32", reason="a pipe closing is not the hangup this watches for")
def test_leaves_a_process_alone_where_no_standard_stream_is_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    shut = os.fdopen(os.open(os.devnull, os.O_RDONLY))
    shut.close()
    reader, writer = os.pipe()
    with os.fdopen(reader) as piped:
        monkeypatch.setattr(sys, "__stdin__", shut)
        for stream in ("__stdout__", "__stderr__"):
            monkeypatch.setattr(sys, stream, piped)
        # The other end of this pipe is closed.
        # A descriptor reports this as it reports a terminal hangup.
        os.close(writer)
        ended = threading.Event()

        end_on_hangup(ended.set)

        assert not ended.wait(STANDS_FOR)
