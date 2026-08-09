import os
import subprocess
import threading
from contextlib import suppress
from typing import TYPE_CHECKING, cast

import pytest

from jri.core.conversation import Conversation
from jri.core.generation import RUNNER_COMMAND, Generation

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from threading import Event

    from jri.core.ai import TurnEvent
    from jri.core.settings import Settings


# The runner as another process, without one. What stands in is the
# spawn and nothing around it, so `Generation.start` runs whole: the
# refusal a lock still held earns, the discard of what a folded run
# left, the wait for the journal, and the reading of a runner that died
# before writing one. `Generation.execute` is the runner's whole life,
# so a thread running it takes the same lock, writes the same journal
# and hears a stop through the same file -- and a lock the operating
# system holds over a file refuses a second taker in the process that
# has it as surely as in any other. What this leaves out is the process
# boundary: a thread shares this process's memory and descriptors and
# dies with it, so a run outliving the window that started it, and a
# runner whose own standard error says why it fell over, are the
# subprocess tests' to prove.
#
# The real runner loads its settings from the workspace, where a test's
# are a double's, so they are taken from the JRI asking for the run at
# the moment it asks.
def run_in_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[Settings] = []
    ralph = Conversation.ralph
    # Kept before the patch below takes its place, and taken as a
    # callable of anything: what reaches it is every call in JRI that
    # starts a process, each with keywords of its own.
    popen = cast("Callable[..., subprocess.Popen[bytes]]", subprocess.Popen)

    def ask(
        conversation: Conversation, cancelled: "Event | None" = None, detached: "Event | None" = None
    ) -> "Generator[TurnEvent]":
        asked.append(conversation.settings)
        return ralph(conversation, cancelled, detached)

    def spawn(command: list[str], **options: object) -> "subprocess.Popen[bytes] | _Runner":
        # Git and the explorer start processes through this same call,
        # and every one of those is a real process still.
        if tuple(command[1:]) != RUNNER_COMMAND:
            return popen(command, **options)
        return _Runner(asked[-1])

    # A hundred milliseconds is what a run reaching a screen is read
    # at; a suite reading a journal a thread beside it wrote is not
    # waiting on a model.
    monkeypatch.setattr(Generation, "POLL", 0.002)
    monkeypatch.setattr(Conversation, "ralph", ask)
    monkeypatch.setattr(subprocess, "Popen", spawn)


# A run the operating system kills leaves a journal with no ending and
# a lock the kernel drops, and no traceback anywhere. A thread standing
# in for that process ends the same way: what a double raises to kill
# it is the kill, not a failure of the suite's.
def _execute_until_killed(settings: "Settings") -> None:
    with suppress(BaseException):
        Generation.execute(settings)


# As much of what `Popen` hands back as the code that spawns a runner
# reads: the number to log it by, and whether it is still going.
class _Runner:
    def __init__(self, settings: "Settings") -> None:
        self.pid = os.getpid()
        self._thread = threading.Thread(target=_execute_until_killed, args=(settings,), daemon=True)
        self._thread.start()

    def poll(self) -> int | None:
        return None if self._thread.is_alive() else 0
