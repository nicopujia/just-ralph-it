import os
import subprocess
import threading
from contextlib import suppress
from typing import TYPE_CHECKING, cast, override

import pytest

from jri.core.conversation import Conversation
from jri.core.generation import RUNNER_COMMAND, Generation
from jri.lib.lock import Lock

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path
    from threading import Event

    from jri.core.ai import TurnEvent
    from jri.core.settings import Settings


# A thread replaces the runner process. This double replaces the spawn and nothing around it, so
# `Generation.spawn` runs in full. It refuses a run while another run holds the lock. It discards what a run that
# did not finish left. It waits for the journal. It reads a runner that died before it wrote a journal.
# `Generation.execute` is the full life of a runner. A thread that runs it takes the same lock, writes the same
# journal, and reads a stop from the same file. An operating system lock on a file refuses a second taker in the
# process that holds it, the same as in any other process.
# A thread gives no process boundary. It shares the memory and the descriptors of this process, and it dies
# with this process. The subprocess tests must show two things. A run continues after the window that started
# it ends, and the standard error of a runner tells why the runner stopped.
#
# The real runner reads its settings from the workspace. In a test a double owns those settings. This double
# takes them from the JRI that asks for the run, at the moment it asks.
def run_in_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[Settings] = []
    ralph = Conversation.ralph
    # Keep the real `Popen` before the patch below replaces it. The type here accepts any arguments, because
    # every call in JRI that starts a process comes here, and each call gives keywords of its own.
    popen = cast("Callable[..., subprocess.Popen[bytes]]", subprocess.Popen)

    def ask(
        conversation: Conversation, cancelled: "Event | None" = None, detached: "Event | None" = None
    ) -> "Generator[TurnEvent]":
        asked.append(conversation.settings)
        return ralph(conversation, cancelled, detached)

    def spawn(command: list[str], **options: object) -> "subprocess.Popen[bytes] | _Runner":
        # Git and the explorer start their processes through this same call. Each of those stays a real process.
        if tuple(command[1:]) != RUNNER_COMMAND:
            return popen(command, **options)
        return _Runner(asked[-1])

    # A run that shows a screen polls every hundred milliseconds. This suite reads a journal that a thread in the
    # same process wrote, and it waits for no model, so it polls much faster.
    monkeypatch.setattr(Generation, "POLL", 0.002)
    monkeypatch.setattr(Conversation, "ralph", ask)
    monkeypatch.setattr(subprocess, "Popen", spawn)


# A runner appends its ending, and only then frees its lock. This lock is free at the first read, and it writes
# that ending at that same read. A follower then finds a free lock over a journal that it did not read to the end.
# This is the one moment where the ending can be lost.
class ConcludingLock(Lock):
    def __init__(self, path: "Path", journal_file: "Path", ending: bytes) -> None:
        super().__init__(path)
        self.journal_file = journal_file
        self.ending = ending

    @override
    def is_held(self) -> bool:
        with self.journal_file.open("ab") as journal:
            journal.write(self.ending)
        # The runner writes one ending. Every later read finds the same free lock over the same journal.
        self.ending = b""
        return False


# The operating system can kill a run. Such a run leaves a journal with no ending, a lock that the kernel frees,
# and no traceback. A thread that replaces that process must end in the same way. An exception that a double
# raises to stop the thread is that kill, and not a failure of the suite.
def _execute_until_killed(settings: "Settings") -> None:
    with suppress(BaseException):
        Generation.execute(settings)


# The code that spawns a runner reads only two things from `Popen`. It reads the number that it logs the
# runner by, and whether the runner is still alive.
class _Runner:
    def __init__(self, settings: "Settings") -> None:
        self.pid = os.getpid()
        self._thread = threading.Thread(target=_execute_until_killed, args=(settings,), daemon=True)
        self._thread.start()

    def poll(self) -> int | None:
        return None if self._thread.is_alive() else 0
