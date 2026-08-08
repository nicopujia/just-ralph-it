import threading
import time
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

from jri.core.conversation import Conversation
from jri.core.generation import Generation

if TYPE_CHECKING:
    from collections.abc import Generator
    from threading import Event

    from jri.core.ai import TurnEvent
    from jri.core.settings import Settings

POLL = 0.005
# A runner that never wrote anything down would hang the suite, and one
# that is going to write has done so in milliseconds.
STARTS_WITHIN = 30.0


# The runner as another process, without one. `Generation.execute` is
# the runner's whole life, so a thread running it takes the same lock,
# writes the same journal and hears a stop through the same file -- and
# a lock the operating system holds over a file refuses a second taker
# in the process that has it as surely as in any other. What this
# leaves out is the spawn itself, which has a subprocess test of its
# own.
#
# The real runner loads its settings from the workspace, where a test's
# are a double's, so they are taken from the JRI asking for the run at
# the moment it asks.
def run_in_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[Settings] = []
    ralph = Conversation.ralph

    def ask(conversation: Conversation, cancelled: "Event | None" = None) -> "Generator[TurnEvent]":
        asked.append(conversation.settings)
        return ralph(conversation, cancelled)

    def start(generation: Generation) -> None:
        generation.workspace.open_generation_dir()
        generation.discard()
        threading.Thread(target=_execute_until_killed, args=(asked[-1],), daemon=True).start()
        deadline = time.monotonic() + STARTS_WITHIN
        while not generation.exists:
            assert time.monotonic() < deadline, "the runner never opened a journal"
            time.sleep(POLL)

    # A hundred milliseconds is what a run reaching a screen is read
    # at; a suite reading a journal a thread beside it wrote is not
    # waiting on a model.
    monkeypatch.setattr(Generation, "POLL", 0.002)
    monkeypatch.setattr(Conversation, "ralph", ask)
    monkeypatch.setattr(Generation, "start", start)


# A run the operating system kills leaves a journal with no ending and
# a lock the kernel drops, and no traceback anywhere. A thread standing
# in for that process ends the same way: what a double raises to kill
# it is the kill, not a failure of the suite's.
def _execute_until_killed(settings: "Settings") -> None:
    with suppress(BaseException):
        Generation.execute(settings)
