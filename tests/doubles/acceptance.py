import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

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
# A reference transaction is not a commit's alone -- `git worktree
# add` opens one too, and a generation opens several before it reaches
# a commit -- so a window named by one waits for the branch the commit
# lands on to be among the refs Git is reporting. They arrive on stdin
# as `<old> <new> <ref>`, one to a line, and are read whole so that Git
# never writes into a pipe the hook has already closed.
REFERENCE_TRANSACTION = '#!/bin/sh\n[ "$1" = {phase} ] || exit 0\ngrep " refs/heads/" >/dev/null || exit 0\n'
# The windows a hook of the project's own opens onto a commit of Git's,
# each named by where Git runs it: a commit of named paths takes the
# index lock before `pre-commit` and keeps it to the end, the ref
# transaction that lands the commit calls `reference-transaction`
# twice -- `prepared` with the locks over HEAD and over the branch
# already taken, then `committed` with the commit written and the index
# Git wrote it from not yet copied over the project's own -- and
# `post-commit` runs past the last of them, where a lock standing
# belongs to whoever took it and to nothing of this commit's. So a hook
# that runs at all is a window Git is in, rather than one a poll has to
# catch.
COMMIT_WINDOWS = {
    "index": ("pre-commit", "#!/bin/sh\n"),
    "branch": ("reference-transaction", REFERENCE_TRANSACTION.format(phase="prepared")),
    "written": ("reference-transaction", REFERENCE_TRANSACTION.format(phase="committed")),
    "past": ("post-commit", "#!/bin/sh\n"),
}
# What a hook makes to say the window it runs in is open.
WINDOW_MARKER = "window-open"
# Ending the Git that ran the hook, which is where an out-of-memory
# kill and a `pkill git` land: the run that started it lives on, and
# neither the signal nor the run is anything Python unwinds from.
KILL_THE_GIT = "kill -9 $PPID\n"
# Standing still in the window instead, long enough for a kill from
# outside to take the whole run down inside it.
HOLD_THE_WINDOW = "sleep 30\n"
# Saying so on the way in, since the locks standing in this window
# either stood before `pre-commit` -- the one over the index -- or
# carry a number of Git's own in the name, so a poll waiting on a lock
# catches the commit at its start or never.
MARK_THE_WINDOW = f"touch .git/{WINDOW_MARKER}\n"
# A second Git taking the lock: the file appears inside the span of a
# command of JRI's, and outlives it, and is none of its business.
TAKE_THE_LOCK = "touch .git/index.lock\n"
# A Git that ends itself at one question and runs the real one at every
# other, so what a run reads is one real death of one real process it
# spawned rather than a status a double made up. `exec` leaves the pid
# JRI spawned as the pid that dies, and the marker arms it, since the
# questions worth killing are asked on the way in as well.
KILLING_GIT = '#!/bin/sh\ncase "$*" in\n  *"{question}") [ -e "{marker}" ] && kill -9 $$ ;;\nesac\nexec "{git}" "$@"\n'
# The question a settlement must not put in front of the one that
# matters: silence is what a Git killed at it leaves, and silence here
# reads as a project holding no commit at all.
HEAD_QUESTION = "rev-parse --verify --quiet HEAD^{commit}"
# Which worktree holds a path, asked once by `find_root` and once, with
# the two directories a `Repository` is built from after it, on the way
# into one. The shim matches the tail of a command line, so each is
# armed on its own.
ROOT_QUESTION = "rev-parse --show-toplevel"
WORKTREE_QUESTION = "rev-parse --show-toplevel --absolute-git-dir --git-common-dir"
POLL = 0.0002
# An acceptance nothing kills is over in well under a second, so this
# is only ever waited out by one that never reached Git at all.
TIMEOUT = 60


# Every lock file Git leaves anywhere under its own directory, read
# off the filesystem rather than asked of the code under test.
def read_git_locks(root: Path) -> tuple[Path, ...]:
    return tuple(sorted((root / ".git").rglob("*.lock")))


# Ahead of the real Git for as long as the test holds the environment,
# and under `.git`, where nothing a run reads ever looks.
def install_a_killing_git(monkeypatch: pytest.MonkeyPatch, root: Path, question: str) -> None:
    executable = shutil.which("git")
    assert executable is not None
    directory = root / ".git" / "killing-git"
    directory.mkdir()
    shim = directory / "git"
    marker = root / ".git" / WINDOW_MARKER
    shim.write_text(KILLING_GIT.format(question=question, marker=marker, git=executable), encoding="utf-8")
    shim.chmod(0o700)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")


def bound_the_acceptance_writes(root: Path, patch: bytes, limit: int) -> str:
    result = subprocess.run(
        [sys.executable, "-B", "-c", BOUNDED_ACCEPTANCE, str(root), str(limit)],
        check=False,
        input=patch,
        capture_output=True,
    )
    return os.fsdecode(result.stderr)


def kill_amid_staging(root: Path, patch: bytes) -> None:
    _kill_inside_a_window(root, patch, "index.lock")


# The far end of the same commit: Git writes the index under one lock
# and then moves the ref the commit lands on under two more, and a run
# killed there leaves locks over HEAD and over the branch as well.
def kill_amid_moving_the_branch(root: Path, patch: bytes) -> None:
    with open_a_commit_window(root, "branch", HOLD_THE_WINDOW):
        _kill_inside_a_window(root, patch, "HEAD.lock")


# Past that end: the ref carries the commit and the project's index is
# still the one the acceptance staged, so a run killed here leaves a
# commit holding specifications beside an index that never heard of
# them.
def kill_amid_writing_the_commit(root: Path, patch: bytes) -> None:
    with open_a_commit_window(root, "written", MARK_THE_WINDOW + HOLD_THE_WINDOW):
        _kill_inside_a_window(root, patch, WINDOW_MARKER)


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


def _kill_inside_a_window(root: Path, patch: bytes, marker: str) -> None:
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
    for awaited in (workspace.acceptance_file, root / ".git" / marker):
        while not awaited.exists():
            assert child.poll() is None, f"the acceptance ended before it reached {awaited.name}"
            assert time.monotonic() < deadline, f"the acceptance never reached {awaited.name}"
            time.sleep(POLL)
    # The whole group: the lock is held by a Git the acceptance
    # started, and killing the acceptance alone leaves that Git to
    # finish and take its lock away again.
    os.killpg(os.getpgid(child.pid), signal.SIGKILL)
    child.wait()
