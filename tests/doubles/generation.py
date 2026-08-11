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


# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
#
# Check this test support.
# Check this test support.
# Check this test support.
def run_in_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[Settings] = []
    ralph = Conversation.ralph
    # Check this test support.
    # Check this test support.
    # Check this test support.
    popen = cast("Callable[..., subprocess.Popen[bytes]]", subprocess.Popen)

    def ask(
        conversation: Conversation, cancelled: "Event | None" = None, detached: "Event | None" = None
    ) -> "Generator[TurnEvent]":
        asked.append(conversation.settings)
        return ralph(conversation, cancelled, detached)

    def spawn(command: list[str], **options: object) -> "subprocess.Popen[bytes] | _Runner":
        # Check this test support.
        # Check this test support.
        if tuple(command[1:]) != RUNNER_COMMAND:
            return popen(command, **options)
        return _Runner(asked[-1])

    # Check this test support.
    # Check this test support.
    # Check this test support.
    monkeypatch.setattr(Generation, "POLL", 0.002)
    monkeypatch.setattr(Conversation, "ralph", ask)
    monkeypatch.setattr(subprocess, "Popen", spawn)


# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
def _execute_until_killed(settings: "Settings") -> None:
    with suppress(BaseException):
        Generation.execute(settings)


# Check this test support.
# Check this test support.
class _Runner:
    def __init__(self, settings: "Settings") -> None:
        self.pid = os.getpid()
        self._thread = threading.Thread(target=_execute_until_killed, args=(settings,), daemon=True)
        self._thread.start()

    def poll(self) -> int | None:
        return None if self._thread.is_alive() else 0
