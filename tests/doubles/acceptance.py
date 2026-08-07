import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from jri.core.workspace import Workspace

if TYPE_CHECKING:
    from collections.abc import Iterator

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
# The same acceptance under a bound the kernel puts on every file the
# process writes from there on, which is what a full disk, a quota or a
# CI file limit is: `git apply` dies inside its own `write(2)`, where no
# Python instruction boundary reaches, and the undo that follows meets
# the same bound the acceptance did. Bytecode stays unwritten so the
# bound is only ever met by a write of the acceptance's own.
BOUNDED_ACCEPTANCE = """
import resource
import sys
from pathlib import Path
from jri.core.specs import Specs

specs = Specs(Path(sys.argv[1]))
baseline = specs.prepare()
patch = sys.stdin.buffer.read()
resource.setrlimit(resource.RLIMIT_FSIZE, (int(sys.argv[2]), resource.getrlimit(resource.RLIMIT_FSIZE)[1]))
specs.accept(patch, baseline)
"""
# The windows a hook of the project's own opens onto a commit of Git's,
# each named by where Git runs it: a commit of named paths takes the
# index lock before `pre-commit` and keeps it to the end, the ref
# transaction that lands the commit calls `reference-transaction` with
# the locks over HEAD and over the branch already taken, and
# `post-commit` runs past the last of them, where a lock standing
# belongs to whoever took it and to nothing of this commit's. So a hook
# that runs at all is a window Git is in, rather than one a poll has to
# catch.
COMMIT_WINDOWS = {
    "index": ("pre-commit", "#!/bin/sh\n"),
    "branch": ("reference-transaction", '#!/bin/sh\n[ "$1" = prepared ] || exit 0\n'),
    "past": ("post-commit", "#!/bin/sh\n"),
}
# Ending the Git that ran the hook, which is where an out-of-memory
# kill and a `pkill git` land: the run that started it lives on, and
# neither the signal nor the run is anything Python unwinds from.
KILL_THE_GIT = "kill -9 $PPID\n"
# Standing still in the window instead, long enough for a kill from
# outside to take the whole run down inside it.
HOLD_THE_WINDOW = "sleep 30\n"
# A second Git taking the lock: the file appears inside the span of a
# command of JRI's, and outlives it, and is none of its business.
TAKE_THE_LOCK = "touch .git/index.lock\n"
POLL = 0.0002
# An acceptance nothing kills is over in well under a second, so this
# is only ever waited out by one that never reached Git at all.
TIMEOUT = 60


# Every lock file Git leaves anywhere under its own directory, read
# off the filesystem rather than asked of the code under test.
def read_git_locks(root: Path) -> tuple[Path, ...]:
    return tuple(sorted((root / ".git").rglob("*.lock")))


def bound_the_acceptance_writes(root: Path, patch: bytes, limit: int) -> str:
    result = subprocess.run(
        [sys.executable, "-B", "-c", BOUNDED_ACCEPTANCE, str(root), str(limit)],
        check=False,
        input=patch,
        capture_output=True,
    )
    return os.fsdecode(result.stderr)


def kill_amid_staging(root: Path, patch: bytes) -> None:
    _kill_amid_locking(root, patch, "index.lock")


# The far end of the same commit: Git writes the index under one lock
# and then moves the ref the commit lands on under two more, and a run
# killed there leaves locks over HEAD and over the branch as well.
def kill_amid_moving_the_branch(root: Path, patch: bytes) -> None:
    with open_a_commit_window(root, "branch", HOLD_THE_WINDOW):
        _kill_amid_locking(root, patch, "HEAD.lock")


@contextmanager
def open_a_commit_window(root: Path, window: str, action: str) -> "Iterator[None]":
    name, preamble = COMMIT_WINDOWS[window]
    hook = root / ".git/hooks" / name
    hook.write_text(preamble + action, encoding="utf-8")
    hook.chmod(0o700)
    try:
        yield
    finally:
        hook.unlink()


def _kill_amid_locking(root: Path, patch: bytes, lock: str) -> None:
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
    for awaited in (workspace.acceptance_file, root / ".git" / lock):
        while not awaited.exists():
            assert child.poll() is None, f"the acceptance ended before it reached {awaited.name}"
            assert time.monotonic() < deadline, f"the acceptance never reached {awaited.name}"
            time.sleep(POLL)
    # The whole group: the lock is held by a Git the acceptance
    # started, and killing the acceptance alone leaves that Git to
    # finish and take its lock away again.
    os.killpg(os.getpgid(child.pid), signal.SIGKILL)
    child.wait()
