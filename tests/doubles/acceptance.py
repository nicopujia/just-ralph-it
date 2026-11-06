import os
import shutil
import signal
import subprocess
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

# A commit is not the only command that opens a reference transaction. `git worktree add` opens one too.
# So a window that a transaction names waits for the branch of the commit among the refs that Git reports.
# Git sends the refs on stdin as `<old> <new> <ref>`, one to a line.
# The hook reads them all, so Git never writes into a pipe that the hook already closed.
REFERENCE_TRANSACTION = '#!/bin/sh\n[ "$1" = {phase} ] || exit 0\ngrep " refs/heads/" >/dev/null || exit 0\n'
# These are the windows that a hook of the project opens on a command of Git. The name of each window tells where
# Git runs the hook. Four windows are in a commit of named paths. Git takes the index lock before `pre-commit`,
# and holds it to the end. The ref transaction that lands the commit calls `reference-transaction` two times. At
# `prepared`, Git already holds the locks over HEAD and over the branch. At `committed`, Git wrote the commit.
# It did not yet copy the index that it wrote the commit from over the index of the project. `post-commit` runs
# past the last of these locks. A lock that stays there belongs to the process that took it, and to no part of
# this commit. The fifth window is in `git worktree add`. That command locks no file of the main worktree at any
# time. Each of those files is free for a second command for the full span. A hook that runs is a window that
# Git is in, and not a window that a poll must catch. A commit of no named paths runs the same hooks, and holds
# the index at none of them. It renames the index that it refreshed over the file in `prepare_index`, before
# `pre-commit`. So `index` names where that lock would be, and not where it is.
HOOK_WINDOWS = {
    "index": ("pre-commit", "#!/bin/sh\n"),
    "branch": ("reference-transaction", REFERENCE_TRANSACTION.format(phase="prepared")),
    "written": ("reference-transaction", REFERENCE_TRANSACTION.format(phase="committed")),
    "past": ("post-commit", "#!/bin/sh\n"),
    "worktree": ("post-checkout", "#!/bin/sh\n"),
}
# A hook makes this file to show that the window it runs in is open.
WINDOW_MARKER = "window-open"
# This ends the Git that ran the hook. An out-of-memory kill and a `pkill git` end it in the same way. The run
# that started the Git stays alive. The signal does not reach Python, so Python unwinds no stack and runs no
# cleanup.
KILL_THE_GIT = "kill -9 $PPID\n"
# A hook stays alive while its test reads the repository. A parallel run loads the machine. This time
# must be much longer than the slowest test. Each test ends its own hook.
HELD_FOR = 300
# This holds each lock that stays while the window is open, where a test reads it back after the run. A window
# that opened where its lock was free leaves the same empty `.git` as one that opened inside that lock and then
# released it. Only what the window saw separates these two. The name ends in no `.lock`, so a count of lock
# files does not count it.
LOCKS_IN_THE_WINDOW = "locks-in-the-window"
RECORD_THE_LOCKS = f"ls .git/*.lock > .git/{LOCKS_IN_THE_WINDOW} 2>/dev/null\n"
# The second command records its pid here. A test reads the pid back, and then knows whether the process that
# holds that lock is still there. The name ends in no `.lock`, so neither `read_git_locks` nor `Locks` counts it.
SECOND_COMMAND_PID = "second-command-pid"
# A second Git takes a lock over one of the files that a command of JRI writes. The lock file appears during that
# command and stays after it, and the command of JRI must leave it alone. The path is full, because a hook does
# not always run where the repository is. `post-checkout` runs in the worktree that `git worktree add` just made.
TAKE_THE_LOCK = "touch {directory}/{lock}\n"
# This is the same lock, but the command that took it still runs when the window closes. The release must answer
# to this state. What stays at the end is a live transaction, and not a leftover. Three parts are necessary. The
# output goes to nothing, because the command gets the pipes that a run reads Git through, and an open pipe holds
# the run open. The command `exec`s the sleep, so the recorded pid is the pid to end. The wait puts the lock in
# the window, and not after the window, where a release never sees it.
HOLD_THE_LOCK = (
    f"sh -c 'touch {{directory}}/{{lock}}; exec sleep {HELD_FOR}' >/dev/null 2>&1 &\n"
    f"echo $! > {{directory}}/{SECOND_COMMAND_PID}\n"
    "until [ -e {directory}/{lock} ]; do :; done\n"
)
# This is a filter of the project. It gives the window in the commands that run no hook of any kind: `git apply`,
# `git add` and a read all put the bytes of a path that the project points at through it. The direction of the
# bytes selects the side that runs. Git runs the smudge side over what it is about to leave in the worktree. Git
# runs the clean side over what it reads back out of the worktree. `cat` makes each side a filter, because Git
# reads the content back off what it ran. Both applies run smudge. An apply that asks for the index took the index
# lock before that point. So the same window is on each side of the one thing that separates the two
# applies. The clean side runs in one apply only. An apply that asks for the index patches the blob that the
# index names, and writes the result straight back as another blob. Its one read of the worktree is the re-hash.
# That re-hash settles whether an entry that Git cannot separate from its index by date still matches. Git makes that
# read only when the entry carries the same second of the clock as the index, and a faster machine avoids it.
WINDOW_FILTER = "window-filter"
FILTERED_PATH = "README.md"
# The hook of the project refuses the commit. Git chooses this end, and runs its exit handler for it. Git exits
# with the 1 that the hook returns.
REFUSE_THE_COMMIT = "exit 1\n"
# This ends the Git that ran the hook with a signal that Git handles. A Ctrl-C over the process group, a `pkill
# git` and a shutdown from a supervisor all end it in the same way. The handler of Git removes its own locks, and
# then lets the default action end it. The kernel still reports the death as a signal.
SIGNAL_THE_GIT = "kill -{name} $PPID\n"
# This is a Git that ends itself at one question, and runs the real Git at each other question. A run then reads
# one real death of one real process that it spawned, and not a status that a double invented. `exec` keeps the
# pid that JRI spawned as the pid that dies. The marker enables the kill, because JRI also asks the questions that
# are worth a kill on the way in.
KILLING_GIT = '#!/bin/sh\ncase "$*" in\n  *"{question}") [ -e "{marker}" ] && kill -9 $$ ;;\nesac\nexec "{git}" "$@"\n'
# A Git that a kill ends here answers nothing, and JRI reads that empty answer as a project that holds no commit.
HEAD_QUESTION = "rev-parse --verify --quiet HEAD^{commit}"
# This question asks which worktree holds a path. `find_root` asks it one time. When JRI enters a worktree, it
# asks it again together with the two directories that it then builds a `Repository` from. The shim matches the
# tail of a command line, so each question matches on its own.
ROOT_QUESTION = "rev-parse --show-toplevel"
WORKTREE_QUESTION = "rev-parse --show-toplevel --absolute-git-dir --git-common-dir"
# This is a staging, written as the shim matches it. A kill here ends Git before it takes the index lock.
# Git reads its configuration as it starts, and takes that lock only after. This is the one window in a command
# of JRI where no hook and no filter of the project can go. The process that dies is the process that JRI
# spawned.
STAGING_QUESTION = "add -- README.md"


# This gives each lock file that Git leaves at any level below its own directory. The test reads the filesystem,
# and does not ask the code under test.
def read_git_locks(root: Path) -> tuple[Path, ...]:
    return tuple(sorted((root / ".git").rglob("*.lock")))


# This gives the same locks, as the window saw them. The names start at the worktree root, because Git runs a
# filter of the project there.
def read_the_locks_the_window_saw(root: Path) -> tuple[str, ...]:
    return tuple((root / ".git" / LOCKS_IN_THE_WINDOW).read_text(encoding="utf-8").split())


# This puts the shim before the real Git for as long as the test holds the environment. The shim goes below
# `.git`, where no read of a run looks.
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


# This tells whether the process that took the lock of the second command is still there. Only such a process
# renames that lock over the file that it guards. Signal zero reaches a live process, and nothing else.
def is_the_second_command_running(root: Path) -> bool:
    try:
        os.kill(_read_the_second_command(root), 0)
    except OSError:
        return False
    return True


def end_the_second_command(root: Path) -> None:
    with suppress(OSError):
        os.kill(_read_the_second_command(root), signal.SIGKILL)


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
def open_a_filter_window(root: Path, action: str, *, side: str, path: str = FILTERED_PATH) -> "Iterator[None]":
    driver = root / ".git" / WINDOW_FILTER
    driver.write_text(f"#!/bin/sh\n{action}cat\n", encoding="utf-8")
    driver.chmod(0o700)
    attributes = root / ".gitattributes"
    attributes.write_text(f"{path} filter={WINDOW_FILTER}\n", encoding="utf-8")
    _configure(root, f"filter.{WINDOW_FILTER}.{side}", str(driver))
    try:
        yield
    finally:
        # The `unlink` below removes this file and closes the window. With no path before them, the driver and
        # the setting reach nothing.
        attributes.unlink()


# This makes a tracked file that the index can decide nothing about from what it recorded. The bytes are the bytes
# that the index already holds, so the size still matches and cannot decide it. The date is a date that no write
# of this run could leave. Git must then read the file back through the clean side of the filter, and hash it. The
# window goes at that read. Neither half is a race, unlike a write that comes inside the second of the index write.
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
