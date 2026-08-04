import subprocess
import time
from pathlib import Path

import pytest


def serve_timeout(monkeypatch: pytest.MonkeyPatch, started: Path) -> None:
    """Time out every bounded wait once the command signals it began.

    Unbounded waits run for real, so the caller can still reap the
    process it killed.
    """

    original_wait = subprocess.Popen.wait

    def wait(process: "subprocess.Popen[bytes]", timeout: float | None = None) -> int:
        if timeout is None:
            return original_wait(process)
        deadline = time.monotonic() + 10
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        raise subprocess.TimeoutExpired(process.args, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", wait)
