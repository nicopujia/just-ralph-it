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

# The acceptance runs in full in a process of its own. The kill below reaches a real `git add`, and not a Python
# instruction boundary. A Python instruction boundary is the one place that never holds `.git/index.lock`.
ACCEPTANCE = """
import sys
from pathlib import Path
from jri.core.specs import Specs

specs = Specs(Path(sys.argv[1]))
specs.accept(sys.stdin.buffer.read(), specs.prepare())
"""
# This is a run of JRI, with only the two things that a halt reads. It opens the generation directory. It takes
# the generation lock under its own pid, as `Generation.execute` does. It then carries out a real acceptance. The
# process leads a session of its own, so the Git of that acceptance is in the group that a halt ends.
RUNNING_ACCEPTANCE = """
import os
import sys
from pathlib import Path
from jri.core.generation import Generation
from jri.core.specs import Specs
from jri.core.workspace import Workspace

root = Path(sys.argv[1])
generation = Generation(Workspace(root))
generation.workspace.open_generation_dir()
assert generation.lock.take(str(os.getpid()))
specs = Specs(root)
specs.accept(sys.stdin.buffer.read(), specs.prepare())
"""
# This is the same acceptance under a bound. The kernel puts that bound on each file that the process writes from
# that point. A full disk, a quota, and a CI file limit all give this bound. `git apply` dies in its own
# `write(2)`, where no Python instruction boundary reaches. The undo that follows meets the same bound. The run
# writes no bytecode, so only a write of the acceptance meets the bound.
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
# A commit is not the only command that opens a reference transaction. `git worktree add` opens one, and a
# generation opens several before it makes a commit. So a window that a transaction names waits for the
# branch of the commit among the refs that Git reports. Git sends the refs on stdin as `<old> <new> <ref>`, one to
# a line. The hook reads them all, so Git never writes into a pipe that the hook already closed.
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
HOLD_THE_WINDOW = f"sleep {HELD_FOR}\n"
# The hook writes this marker as it starts. A poll waits for the marker where a lock will not do. In the commit,
# a lock that stays is one of two locks. It is the lock over the index, which Git took before `pre-commit`, or a
# lock that carries a number of Git in its name. A poll on a lock catches that commit at its start, or never.
# In `git add`, the lock is the same lock that the kill is about. It stays for only a moment, whether the window
# opened or not. Only the marker separates a kill inside the window from a kill that raced it.
MARK_THE_WINDOW = f"touch .git/{WINDOW_MARKER}\n"
# The Git that runs the hook writes down its own pid. A test reads that pid back. It can then tell whether a
# kill of the group reached the child of the run, and not the run alone. The name ends in no `.lock`, so a count
# of lock files does not count it.
GIT_IN_THE_WINDOW = "git-in-the-window"
RECORD_THE_GIT = f"echo $PPID > .git/{GIT_IN_THE_WINDOW}\n"
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
# This is a monitor of the project. It gives the one window in `git add`. Git takes the index lock before it reads
# the index, and at that read Git asks a monitor what the worktree changed since. Each other command that an
# acceptance runs asks the same question with nothing locked: the apply, and each read before it. A held index
# lock separates the staging from those. A monitor that holds only there puts the window in the one command whose
# lock a kill must leave behind. Git takes an empty answer as a warning, and then reads the worktree itself. Git
# takes a refusal as the same read without the warning. A refusal lets each other command through.
WINDOW_MONITOR = "window-monitor"
MONITOR_THE_INDEX_LOCK = "#!/bin/sh\n[ -e {directory}/index.lock ] || exit 1\n"
# The hook of the project refuses the commit. Git chooses this end, and runs its exit handler for it. Git exits
# with the 1 that the hook returns.
REFUSE_THE_COMMIT = "exit 1\n"
# This is a commit of the user, stopped where each commit stops longest: in the editor. Git takes the index lock
# before the editor, and holds it until it writes the commit. A live process renames the lock that stays while
# this waits over the index. This state separates a live lock from a leftover lock. The file alone does not.
COMMIT_EDITOR = '#!/bin/sh\necho "{message}" > "$1"\nuntil [ -e "{closed}" ]; do sleep 0.02; done\n'
# The editor waits for this file. The commit then stays open for as long as a test needs it, and not for a fixed
# time that a slow machine passes.
EDITOR_CLOSED = "editor-closed"
USER_COMMIT = "the user's own commit"
# This ends the Git that ran the hook with a signal that Git handles. A Ctrl-C over the process group, a `pkill
# git` and a shutdown from a supervisor all end it in the same way. The handler of Git removes its own locks, and
# then lets the default action end it. The kernel still reports the death as a signal.
SIGNAL_THE_GIT = "kill -{name} $PPID\n"
# This is a Git that ends itself at one question, and runs the real Git at each other question. A run then reads
# one real death of one real process that it spawned, and not a status that a double invented. `exec` keeps the
# pid that JRI spawned as the pid that dies. The marker enables the kill, because JRI also asks the questions that
# are worth a kill on the way in.
KILLING_GIT = '#!/bin/sh\ncase "$*" in\n  *"{question}") [ -e "{marker}" ] && kill -9 $$ ;;\nesac\nexec "{git}" "$@"\n'
# A settlement must not ask this question before the question that matters. A Git that a kill ends here answers
# nothing, and JRI reads that empty answer as a project that holds no commit at all.
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
POLL = 0.0002
# An acceptance that nothing kills ends in much less than a second. Only an acceptance that never reached Git
# uses all of this time.
TIMEOUT = 60


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


def bound_the_acceptance_writes(root: Path, patch: bytes, limit: int) -> str:
    result = subprocess.run(
        [sys.executable, "-B", "-c", BOUNDED_ACCEPTANCE, str(root), str(limit)],
        check=False,
        input=patch,
        capture_output=True,
    )
    return os.fsdecode(result.stderr)


def kill_amid_staging(root: Path, patch: bytes) -> None:
    with _open_a_monitor_window(root, MARK_THE_WINDOW + HOLD_THE_WINDOW):
        _kill_inside_a_window(root, patch, WINDOW_MARKER)


# This is a later point in the same commit. Git writes the index under one lock. Git then moves the ref that the
# commit names under two more locks. A run that a kill ends there also leaves locks over HEAD and the branch.
def kill_amid_moving_the_branch(root: Path, patch: bytes) -> None:
    with open_a_window(root, "branch", HOLD_THE_WINDOW):
        _kill_inside_a_window(root, patch, "HEAD.lock")


# This is a point after that one. The ref carries the commit, but the index of the project is still the index
# that the acceptance staged. A run that a kill ends here leaves a commit that holds specifications, and an index
# that does not know them.
def kill_amid_writing_the_commit(root: Path, patch: bytes) -> None:
    with open_a_window(root, "written", MARK_THE_WINDOW + HOLD_THE_WINDOW):
        _kill_inside_a_window(root, patch, WINDOW_MARKER)


# This is a run of JRI, alive in a process group of its own, inside the window that the caller opened around it.
# The window marks itself, because the record and the acceptance lock stay whether that window opened or not.
@contextmanager
def hold_a_run_amid_accepting(root: Path, patch: bytes) -> "Iterator[subprocess.Popen[bytes]]":
    runner = subprocess.Popen(
        [sys.executable, "-c", RUNNING_ACCEPTANCE, str(root)], stdin=subprocess.PIPE, start_new_session=True
    )
    assert runner.stdin is not None
    runner.stdin.write(patch)
    runner.stdin.close()
    deadline = time.monotonic() + TIMEOUT
    # The record comes first. The window is the second thing to wait for, and not a lock that an earlier read of
    # Git holds.
    for awaited in (Workspace(root).acceptance_file, root / ".git" / WINDOW_MARKER):
        while not awaited.exists():
            assert runner.poll() is None, f"the run ended before it reached {awaited.name}"
            assert time.monotonic() < deadline, f"the run never reached {awaited.name}"
            time.sleep(POLL)
    try:
        yield runner
    finally:
        # A test that ended this group already leaves nothing here to end.
        with suppress(OSError):
            os.killpg(os.getpgid(runner.pid), signal.SIGKILL)
        runner.wait()


# This gives the Git that the window is open in. A test reads it, and then waits for that process to end.
def read_the_git_in_the_window(root: Path) -> int:
    return int((root / ".git" / GIT_IN_THE_WINDOW).read_text(encoding="utf-8"))


# This is a Git of the user. It stays alive and holds the index lock for as long as the block lasts. It holds the
# lock for its own write of the index. A run that takes the lock away costs it that write.
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


@contextmanager
def _open_a_monitor_window(root: Path, action: str) -> "Iterator[None]":
    directory = root / ".git"
    monitor = directory / WINDOW_MONITOR
    monitor.write_text(MONITOR_THE_INDEX_LOCK.format(directory=directory) + action, encoding="utf-8")
    monitor.chmod(0o700)
    _configure(root, "core.fsmonitor", str(monitor))
    try:
        yield
    finally:
        # The setting closes this window. A monitor that Git no longer knows about is a file below `.git` like any
        # other.
        _configure(root, "--unset", "core.fsmonitor")


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
    # The record comes first. The kill waits for the lock of the acceptance, and not for a lock that an
    # earlier read of Git holds.
    for awaited in (workspace.acceptance_file, root / ".git" / marker):
        while not awaited.exists():
            assert child.poll() is None, f"the acceptance ended before it reached {awaited.name}"
            assert time.monotonic() < deadline, f"the acceptance never reached {awaited.name}"
            time.sleep(POLL)
    # The kill goes to the whole group. A Git that the acceptance started holds the lock. A kill of the acceptance
    # alone leaves that Git to finish and remove its lock again.
    os.killpg(os.getpgid(child.pid), signal.SIGKILL)
    child.wait()
