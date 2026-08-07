import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from jri.core.workspace import Workspace
from jri.lib import git

# The acceptance runs whole, in a process of its own, so the kill below
# lands on a real `git add` rather than at a Python instruction
# boundary, which is the one place `.git/index.lock` is never held.
ACCEPTANCE = """
import sys
from pathlib import Path
from jri.core.specs import Specs

specs = Specs(Path(sys.argv[1]))
specs.accept(sys.stdin.buffer.read(), specs.prepare())
"""
POLL = 0.0002
# An acceptance nothing kills is over in well under a second, so this
# is only ever waited out by one that never reached Git at all.
TIMEOUT = 60


def kill_amid_staging(root: Path, patch: bytes) -> None:
    workspace = Workspace(root)
    child = subprocess.Popen(
        [sys.executable, "-c", ACCEPTANCE, str(root)], stdin=subprocess.PIPE, start_new_session=True
    )
    assert child.stdin is not None
    child.stdin.write(patch)
    child.stdin.close()
    deadline = time.monotonic() + TIMEOUT
    # The record first, so the kill is waiting for the acceptance's own
    # lock rather than for one an earlier read of Git's might hold.
    for awaited in (workspace.acceptance_file, git.Repository(root).index_lock_file):
        while not awaited.exists():
            assert child.poll() is None, f"the acceptance ended before it reached {awaited.name}"
            assert time.monotonic() < deadline, f"the acceptance never reached {awaited.name}"
            time.sleep(POLL)
    # The whole group: the lock is held by a Git the acceptance
    # started, and killing the acceptance alone leaves that Git to
    # finish and take its lock away again.
    os.killpg(os.getpgid(child.pid), signal.SIGKILL)
    child.wait()
