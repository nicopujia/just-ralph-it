import os
import sys
import threading

import pytest

from jri.lib.terminal import end_on_hangup

ANSWERS_WITHIN = 5.0
# Long enough for the watch to have looked more than once.
STANDS_FOR = 1.0


@pytest.mark.skipif(sys.platform == "win32", reason="a pseudo-terminal is the one terminal a test can hang up")
def test_answers_the_hangup_of_the_terminal_the_process_was_started_in(monkeypatch: pytest.MonkeyPatch) -> None:
    # `os.openpty` rather than `pty.openpty`: importing `pty` reaches
    # `termios`, which Windows has not, and a module that fails to
    # import is a collection error the skip above never answers.
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
        # The far side of the pipe is gone, which a descriptor reports
        # exactly as a terminal reports a hangup.
        os.close(writer)
        ended = threading.Event()

        end_on_hangup(ended.set)

        assert not ended.wait(STANDS_FOR)
