import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
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
# The windows a hook of the project's own opens onto a command of
# Git's, each named by where Git runs it. Four are inside a commit of
# named paths: it takes the index lock before `pre-commit` and keeps it
# to the end, the ref transaction that lands the commit calls
# `reference-transaction` twice -- `prepared` with the locks over HEAD
# and over the branch already taken, then `committed` with the commit
# written and the index Git wrote it from not yet copied over the
# project's own -- and `post-commit` runs past the last of them, where a
# lock standing belongs to whoever took it and to nothing of this
# commit's. The fifth is inside `git worktree add`, which locks nothing
# of the main worktree's at any point, so every one of those files is
# free for a second command for the whole span. A hook that runs at all
# is a window Git is in, rather than one a poll has to catch. A commit
# of no named paths runs the same hooks and holds the index at none of
# them: it renames the index it refreshed over the file inside
# `prepare_index`, ahead of `pre-commit`, so `index` names where that
# lock would be rather than where it is.
HOOK_WINDOWS = {
    "index": ("pre-commit", "#!/bin/sh\n"),
    "branch": ("reference-transaction", REFERENCE_TRANSACTION.format(phase="prepared")),
    "written": ("reference-transaction", REFERENCE_TRANSACTION.format(phase="committed")),
    "past": ("post-commit", "#!/bin/sh\n"),
    "worktree": ("post-checkout", "#!/bin/sh\n"),
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
# Saying so on the way in, which is what a poll waits for wherever a
# lock will not do. Inside the commit, the locks standing either stood
# before `pre-commit` -- the one over the index -- or carry a number of
# Git's own in the name, so a poll on a lock catches that commit at its
# start or never. Inside `git add`, the lock is the very one the kill
# is about, but it stands for a moment whether the window opened or
# not, so only the marker tells a kill held inside it from one that
# raced it.
MARK_THE_WINDOW = f"touch .git/{WINDOW_MARKER}\n"
# Every lock standing while the window is open, written down where a
# test can read it back after the run. A window that opened where the
# lock it is about was free leaves the same empty `.git` behind as one
# that opened inside that lock and had it released, so only what the
# window itself saw tells those apart. Not a `.lock`, so nothing
# counting those counts it.
LOCKS_IN_THE_WINDOW = "locks-in-the-window"
RECORD_THE_LOCKS = f"ls .git/*.lock > .git/{LOCKS_IN_THE_WINDOW} 2>/dev/null\n"
# Where the second command records itself, so a test can read back
# whether the process holding that lock is still there. Not a `.lock`,
# so neither `read_git_locks` nor `Locks` ever counts it.
SECOND_COMMAND_PID = "second-command-pid"
# A second Git taking a lock over one of the files a command of JRI's
# writes: the file appears inside the span of that command, and
# outlives it, and is none of its business. Named in full because a
# hook does not always run where the repository is -- `post-checkout`
# runs in the worktree `git worktree add` has just made.
TAKE_THE_LOCK = "touch {directory}/{lock}\n"
# The same lock taken by a command that is still running when the
# window closes, which is the state the release is answerable to: what
# stands at the end is a live transaction, not a leftover. Three
# things are load-bearing. Its output goes nowhere, since it inherits
# the pipes a run reads Git through and holding those open would hold
# the run open. It `exec`s the sleep, so the pid recorded is the pid
# that has to be ended. And the wait is what puts the lock inside the
# window rather than after it, where a release would never see it.
HOLD_THE_LOCK = (
    "sh -c 'touch {directory}/{lock}; exec sleep 30' >/dev/null 2>&1 &\n"
    f"echo $! > {{directory}}/{SECOND_COMMAND_PID}\n"
    "until [ -e {directory}/{lock} ]; do :; done\n"
)
# A filter of the project's own, which is the window inside the
# commands that run no hook of any kind: `git apply`, `git add` and a
# read all put the bytes of a path the project points at one through
# it. Which side of it runs is which way the bytes are going -- the
# smudge side over what Git is about to leave in the worktree, the
# clean side over what it is reading back out of one -- and `cat` is
# what makes either a filter, since Git reads the content back off
# what it ran. Both applies smudge, and where the apply was asked for
# the index it has taken the index lock before it gets that far, so
# the same window stands either side of the one thing that tells those
# two applies apart. The clean side stands inside an apply for one of
# them only: an apply asked for the index patches the blob the index
# already names and writes the result straight back as another, so its
# one read of the worktree is the re-hash settling whether an entry
# Git cannot date apart from its index still matches -- which the same
# second of the clock arms and a faster machine disarms.
WINDOW_FILTER = "window-filter"
FILTERED_PATH = "README.md"
# A monitor of the project's own, which is the one window inside `git
# add`: Git takes the index lock before it reads the index, and
# reading it is where it asks a monitor what the worktree has done
# since. Every other command an acceptance runs asks that same
# question with nothing locked -- the apply, and every read ahead of
# it -- so the index lock standing is what tells the staging apart
# from those, and a monitor that holds only there puts the window
# inside the one command whose lock a kill is meant to leave behind.
# What Git makes of an empty answer is a warning on its way to reading
# the worktree itself, and what it makes of a refusal is the same read
# without the warning, so a refusal is how every other command is let
# by.
WINDOW_MONITOR = "window-monitor"
MONITOR_THE_INDEX_LOCK = "#!/bin/sh\n[ -e {directory}/index.lock ] || exit 1\n"
# The project's own hook refusing the commit, which is an ending Git
# chooses and runs its exit handler for: the hook's 1 is Git's 1.
REFUSE_THE_COMMIT = "exit 1\n"
# A commit of the user's own, standing where every commit stands
# longest: in the editor. Git takes the index lock ahead of the editor
# and holds it until the commit is written, so the lock standing while
# this waits is one a live process is still going to rename over the
# index -- the state a leftover lock is told from and cannot be told
# from by the file alone.
COMMIT_EDITOR = '#!/bin/sh\necho "{message}" > "$1"\nuntil [ -e "{closed}" ]; do sleep 0.02; done\n'
# What the editor waits for, so the commit is held for exactly as long
# as a test needs it rather than for a time a slow machine outruns.
EDITOR_CLOSED = "editor-closed"
USER_COMMIT = "the user's own commit"
# Ending the Git that ran the hook with a signal Git is asked to stop
# at, which is where a Ctrl-C over the process group, a `pkill git` and
# a supervisor's shutdown land. Git's handler takes its own locks away
# and then lets the default action end it, so the kernel still reports
# the death as a signal.
SIGNAL_THE_GIT = "kill -{name} $PPID\n"
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
# A staging, spelled as the shim matches it. Killing a Git here ends it
# before it has reached for the index lock: Git reads its configuration
# on the way in and only then takes that lock, so this is the one window
# inside a command of JRI's that no hook and no filter of the project's
# own can be put in, the process that dies being the one JRI spawned.
STAGING_QUESTION = "add -- README.md"
POLL = 0.0002
# An acceptance nothing kills is over in well under a second, so this
# is only ever waited out by one that never reached Git at all.
TIMEOUT = 60


# Every lock file Git leaves anywhere under its own directory, read
# off the filesystem rather than asked of the code under test.
def read_git_locks(root: Path) -> tuple[Path, ...]:
    return tuple(sorted((root / ".git").rglob("*.lock")))


# The same, as the window saw it: named from the worktree root, which
# is where Git runs a filter of the project's own.
def read_the_locks_the_window_saw(root: Path) -> tuple[str, ...]:
    return tuple((root / ".git" / LOCKS_IN_THE_WINDOW).read_text(encoding="utf-8").split())


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


# Whether the process that took the second command's lock is still
# there to rename it over the file it guards: signal nought reaches a
# live process and nothing else.
def is_the_second_command_running(root: Path) -> bool:
    try:
        os.kill(_read_the_second_command(root), 0)
    except OSError:
        return False
    return True


def end_the_second_command(root: Path) -> None:
    with suppress(OSError):
        os.kill(_read_the_second_command(root), signal.SIGKILL)


def bound_the_acceptance_writes(root: Path, patch: bytes, limit: int) -> str:
    result = subprocess.run(
        [sys.executable, "-B", "-c", BOUNDED_ACCEPTANCE, str(root), str(limit)],
        check=False,
        input=patch,
        capture_output=True,
    )
    return os.fsdecode(result.stderr)


def kill_amid_staging(root: Path, patch: bytes) -> None:
    with open_a_monitor_window(root, MARK_THE_WINDOW + HOLD_THE_WINDOW):
        _kill_inside_a_window(root, patch, WINDOW_MARKER)


# The far end of the same commit: Git writes the index under one lock
# and then moves the ref the commit lands on under two more, and a run
# killed there leaves locks over HEAD and over the branch as well.
def kill_amid_moving_the_branch(root: Path, patch: bytes) -> None:
    with open_a_window(root, "branch", HOLD_THE_WINDOW):
        _kill_inside_a_window(root, patch, "HEAD.lock")


# Past that end: the ref carries the commit and the project's index is
# still the one the acceptance staged, so a run killed here leaves a
# commit holding specifications beside an index that never heard of
# them.
def kill_amid_writing_the_commit(root: Path, patch: bytes) -> None:
    with open_a_window(root, "written", MARK_THE_WINDOW + HOLD_THE_WINDOW):
        _kill_inside_a_window(root, patch, WINDOW_MARKER)


# A Git of the user's, live and holding the index lock, for as long as
# the block lasts. What it is holding it for is its own write of the
# index, which is what a run that takes the lock away costs it.
@contextmanager
def hold_a_commit_of_the_user_s(root: Path) -> "Iterator[subprocess.Popen[bytes]]":
    executable = shutil.which("git")
    assert executable is not None
    closed = root / ".git" / EDITOR_CLOSED
    editor = root / ".git/commit-editor"
    editor.write_text(COMMIT_EDITOR.format(message=USER_COMMIT, closed=closed), encoding="utf-8")
    editor.chmod(0o700)
    (root / "README.md").write_bytes(b"# Project\nA line of the user's own.\n")
    commit = subprocess.Popen(
        [executable, "-C", str(root), "commit", "-a"],
        env={**os.environ, "GIT_EDITOR": str(editor)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + TIMEOUT
    while not (root / ".git/index.lock").exists():
        assert commit.poll() is None, "the commit ended before it took the index lock"
        assert time.monotonic() < deadline, "the commit never took the index lock"
        time.sleep(POLL)
    try:
        yield commit
    finally:
        closed.touch()
        commit.communicate(timeout=TIMEOUT)


@contextmanager
def open_a_window(root: Path, window: str, action: str) -> "Iterator[None]":
    name, preamble = HOOK_WINDOWS[window]
    hook = root / ".git/hooks" / name
    hook.write_text(preamble + action, encoding="utf-8")
    hook.chmod(0o700)
    try:
        yield
    finally:
        hook.unlink()


@contextmanager
def open_a_filter_window(root: Path, action: str, *, side: str) -> "Iterator[None]":
    driver = root / ".git" / WINDOW_FILTER
    driver.write_text(f"#!/bin/sh\n{action}cat\n", encoding="utf-8")
    driver.chmod(0o700)
    attributes = root / ".gitattributes"
    attributes.write_text(f"{FILTERED_PATH} filter={WINDOW_FILTER}\n", encoding="utf-8")
    _configure(root, f"filter.{WINDOW_FILTER}.{side}", str(driver))
    try:
        yield
    finally:
        # What closes the window is this file: the driver and the
        # setting reach nothing with no path put in front of them.
        attributes.unlink()


@contextmanager
def open_a_monitor_window(root: Path, action: str) -> "Iterator[None]":
    directory = root / ".git"
    monitor = directory / WINDOW_MONITOR
    monitor.write_text(MONITOR_THE_INDEX_LOCK.format(directory=directory) + action, encoding="utf-8")
    monitor.chmod(0o700)
    _configure(root, "core.fsmonitor", str(monitor))
    try:
        yield
    finally:
        # What closes this one is the setting: a monitor Git no longer
        # knows about is a file under `.git` like any other.
        _configure(root, "--unset", "core.fsmonitor")


# A tracked file the index can tell nothing about from what it recorded
# of it: the bytes are the bytes the index already holds, so the size
# still matches and cannot decide it, and the date is one no write of
# this run's could have left. What is left for Git is to read the file
# back through the clean side of the filter and hash it, which is what
# puts a window inside a read. Neither half is a race, unlike a write
# landing in the second the index was written in.
def stale_the_filtered_path(root: Path) -> None:
    path = root / FILTERED_PATH
    path.write_bytes(path.read_bytes())
    os.utime(path, (0, 0))


def _configure(root: Path, *setting: str) -> None:
    executable = shutil.which("git")
    assert executable is not None
    subprocess.run([executable, "-C", str(root), "config", *setting], check=True, capture_output=True)


def _read_the_second_command(root: Path) -> int:
    return int((root / ".git" / SECOND_COMMAND_PID).read_text(encoding="utf-8"))


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
