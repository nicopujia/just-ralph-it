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

# Check this test support.
# Check this test support.
# Check this test support.
ACCEPTANCE = """
import sys
from pathlib import Path
from jri.core.specs import Specs

specs = Specs(Path(sys.argv[1]))
specs.accept(sys.stdin.buffer.read(), specs.prepare())
"""
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
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
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
REFERENCE_TRANSACTION = '#!/bin/sh\n[ "$1" = {phase} ] || exit 0\ngrep " refs/heads/" >/dev/null || exit 0\n'
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
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
HOOK_WINDOWS = {
    "index": ("pre-commit", "#!/bin/sh\n"),
    "branch": ("reference-transaction", REFERENCE_TRANSACTION.format(phase="prepared")),
    "written": ("reference-transaction", REFERENCE_TRANSACTION.format(phase="committed")),
    "past": ("post-commit", "#!/bin/sh\n"),
    "worktree": ("post-checkout", "#!/bin/sh\n"),
}
# Check this test support.
WINDOW_MARKER = "window-open"
# Check this test support.
# Check this test support.
# Check this test support.
KILL_THE_GIT = "kill -9 $PPID\n"
# A hook stays alive while its test reads the repository. A parallel run loads the machine, thus this window
# must outlive the slowest test by a large margin. Each test ends its own hook.
HELD_FOR = 300
# Check this test support.
# Check this test support.
HOLD_THE_WINDOW = f"sleep {HELD_FOR}\n"
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
MARK_THE_WINDOW = f"touch .git/{WINDOW_MARKER}\n"
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
LOCKS_IN_THE_WINDOW = "locks-in-the-window"
RECORD_THE_LOCKS = f"ls .git/*.lock > .git/{LOCKS_IN_THE_WINDOW} 2>/dev/null\n"
# Check this test support.
# Check this test support.
# Check this test support.
SECOND_COMMAND_PID = "second-command-pid"
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
TAKE_THE_LOCK = "touch {directory}/{lock}\n"
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
HOLD_THE_LOCK = (
    f"sh -c 'touch {{directory}}/{{lock}}; exec sleep {HELD_FOR}' >/dev/null 2>&1 &\n"
    f"echo $! > {{directory}}/{SECOND_COMMAND_PID}\n"
    "until [ -e {directory}/{lock} ]; do :; done\n"
)
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
# Check this test support.
# Check this test support.
# Check this test support.
WINDOW_FILTER = "window-filter"
FILTERED_PATH = "README.md"
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
WINDOW_MONITOR = "window-monitor"
MONITOR_THE_INDEX_LOCK = "#!/bin/sh\n[ -e {directory}/index.lock ] || exit 1\n"
# Check this test support.
# Check this test support.
REFUSE_THE_COMMIT = "exit 1\n"
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
COMMIT_EDITOR = '#!/bin/sh\necho "{message}" > "$1"\nuntil [ -e "{closed}" ]; do sleep 0.02; done\n'
# Check this test support.
# Check this test support.
EDITOR_CLOSED = "editor-closed"
USER_COMMIT = "the user's own commit"
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
SIGNAL_THE_GIT = "kill -{name} $PPID\n"
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
KILLING_GIT = '#!/bin/sh\ncase "$*" in\n  *"{question}") [ -e "{marker}" ] && kill -9 $$ ;;\nesac\nexec "{git}" "$@"\n'
# Check this test support.
# Check this test support.
# Check this test support.
HEAD_QUESTION = "rev-parse --verify --quiet HEAD^{commit}"
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
ROOT_QUESTION = "rev-parse --show-toplevel"
WORKTREE_QUESTION = "rev-parse --show-toplevel --absolute-git-dir --git-common-dir"
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
STAGING_QUESTION = "add -- README.md"
POLL = 0.0002
# Check this test support.
# Check this test support.
TIMEOUT = 60


# Check this test support.
# Check this test support.
def read_git_locks(root: Path) -> tuple[Path, ...]:
    return tuple(sorted((root / ".git").rglob("*.lock")))


# Check this test support.
# Check this test support.
def read_the_locks_the_window_saw(root: Path) -> tuple[str, ...]:
    return tuple((root / ".git" / LOCKS_IN_THE_WINDOW).read_text(encoding="utf-8").split())


# Check this test support.
# Check this test support.
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


# Check this test support.
# Check this test support.
# Check this test support.
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


# Check this test support.
# Check this test support.
# Check this test support.
def kill_amid_moving_the_branch(root: Path, patch: bytes) -> None:
    with open_a_window(root, "branch", HOLD_THE_WINDOW):
        _kill_inside_a_window(root, patch, "HEAD.lock")


# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
def kill_amid_writing_the_commit(root: Path, patch: bytes) -> None:
    with open_a_window(root, "written", MARK_THE_WINDOW + HOLD_THE_WINDOW):
        _kill_inside_a_window(root, patch, WINDOW_MARKER)


# Check this test support.
# Check this test support.
# Check this test support.
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
        # Check this test support.
        # Check this test support.
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
        # Check this test support.
        # Check this test support.
        _configure(root, "--unset", "core.fsmonitor")


# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
# Check this test support.
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
    # Check this test support.
    # Check this test support.
    for awaited in (workspace.acceptance_file, root / ".git" / marker):
        while not awaited.exists():
            assert child.poll() is None, f"the acceptance ended before it reached {awaited.name}"
            assert time.monotonic() < deadline, f"the acceptance never reached {awaited.name}"
            time.sleep(POLL)
    # Check this test support.
    # Check this test support.
    # Check this test support.
    os.killpg(os.getpgid(child.pid), signal.SIGKILL)
    child.wait()
