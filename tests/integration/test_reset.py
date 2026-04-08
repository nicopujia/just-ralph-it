import subprocess
import subprocess as subprocess_module
import sys
from pathlib import Path
from typing import cast

import pytest

from jri.core.errors import JriError
from jri.core.models import AttemptState, OpenCodeRunResult, ProcessState, State
from jri.core.opencode import OpenCodeClient
from jri.core.service import JriService
from tests.conftest import run_cli
from tests.helpers import git, read_json, write_task


class SuccessfulFakeOpenCodeClient(OpenCodeClient):
    def __init__(self) -> None:
        super().__init__(model=None)
        self.calls: list[tuple[str, Path]] = []

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> OpenCodeRunResult:
        self.calls.append((prompt, log_path))
        (root / "implemented.txt").write_text("implemented\n", encoding="utf-8")
        log_path.write_text("fake run\n", encoding="utf-8")
        return OpenCodeRunResult(
            returncode=0, session_id="ses_fake", outcome="completed"
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake"}\n', encoding="utf-8")


class FailedFakeOpenCodeClient(OpenCodeClient):
    def __init__(self) -> None:
        super().__init__(model=None)
        self.calls: list[tuple[str, Path]] = []

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> OpenCodeRunResult:
        self.calls.append((prompt, log_path))
        log_path.write_text("fake failed run\n", encoding="utf-8")
        return OpenCodeRunResult(
            returncode=0, session_id="ses_failed", outcome="failed"
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake_failed"}\n', encoding="utf-8")


class DistinctFileFakeOpenCodeClient(OpenCodeClient):
    def __init__(self) -> None:
        super().__init__(model=None)
        self.calls: list[tuple[str, Path]] = []

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> OpenCodeRunResult:
        self.calls.append((prompt, log_path))
        filename = "first.txt" if len(self.calls) == 1 else "second.txt"
        (root / filename).write_text(f"{filename}\n", encoding="utf-8")
        git(root, "add", filename)
        git(root, "commit", "-m", f"add {filename}")
        log_path.write_text(f"fake {filename} run\n", encoding="utf-8")
        return OpenCodeRunResult(
            returncode=0,
            session_id=f"ses_{len(self.calls)}",
            outcome="completed",
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text(
            f'{{"session": "{session_id}"}}\n', encoding="utf-8"
        )


def _dead_pid() -> int:
    process = subprocess.Popen(["sleep", "0"])
    process.wait(timeout=5)
    return process.pid


def _run_successful_task(repo: Path) -> JriService:
    write_task(
        repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(repo, "add", ".jri/tasks/todo/implement-file.md")
    git(repo, "commit", "-m", "add task")
    service = JriService(repo, opencode_client=SuccessfulFakeOpenCodeClient())
    assert service.start(max_tasks=1) == 1
    return service


def test_reset_after_successful_task(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = _run_successful_task(git_repo)
    service.state_store.save_session("ses_reset_test")

    (git_repo / "extra.txt").write_text("extra content\n", encoding="utf-8")
    git(git_repo, "add", "extra.txt")
    git(git_repo, "commit", "-m", "add extra file")

    service.reset()

    assert not (git_repo / "extra.txt").exists()
    assert (git_repo / "implemented.txt").read_text(encoding="utf-8") == "implemented\n"
    assert git(git_repo, "branch", "--show-current") == "main"

    state = read_json(git_repo / ".jri" / "state.json")
    assert "finished_at" in state
    assert state["session"] == "ses_reset_test"
    assert "process" not in state
    assert "active_attempt" not in state


def test_reset_prefers_latest_end_tag_over_jri_init_fallback(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="task-a",
        title="Task A",
        priority=0,
        assignee="Ralph",
        body="Complete task A.",
    )
    write_task(
        git_repo,
        status="todo",
        slug="task-b",
        title="Task B",
        priority=0,
        assignee="Ralph",
        body="Complete task B.",
    )
    git(git_repo, "add", ".jri/tasks/todo")
    git(git_repo, "commit", "-m", "add two tasks")

    service = JriService(git_repo, opencode_client=DistinctFileFakeOpenCodeClient())

    assert service.start(max_tasks=2, force=True) == 2

    (git_repo / "extra.txt").write_text("extra\n", encoding="utf-8")
    git(git_repo, "add", "extra.txt")
    git(git_repo, "commit", "-m", "add extra file")

    service.reset()

    assert (git_repo / "first.txt").exists()
    assert (git_repo / "second.txt").exists()
    assert not (git_repo / "extra.txt").exists()
    assert git(git_repo, "branch", "--show-current") == "main"


def test_reset_after_failed_task(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="task-a",
        title="Task A",
        priority=0,
        assignee="Ralph",
        body="Complete task A.",
    )
    git(git_repo, "add", ".jri/tasks/todo/task-a.md")
    git(git_repo, "commit", "-m", "add task-a")

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())
    assert service.start(max_tasks=1) == 1

    write_task(
        git_repo,
        status="todo",
        slug="task-b",
        title="Task B",
        priority=0,
        assignee="Ralph",
        body="This will fail.",
    )
    git(git_repo, "add", ".jri/tasks/todo/task-b.md")
    git(git_repo, "commit", "-m", "add task-b")

    fail_service = JriService(git_repo, opencode_client=FailedFakeOpenCodeClient())
    assert fail_service.start(max_tasks=1) == 0

    (git_repo / "extra-after-fail.txt").write_text("extra\n", encoding="utf-8")
    git(git_repo, "add", "extra-after-fail.txt")
    git(git_repo, "commit", "-m", "extra after failure")

    service.reset()

    assert not (git_repo / "extra-after-fail.txt").exists()
    assert (git_repo / "implemented.txt").read_text(encoding="utf-8") == "implemented\n"
    assert git(git_repo, "branch", "--show-current") == "main"


def test_reset_from_feature_branch_with_stale_state(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="first-task",
        title="First task",
        priority=0,
        assignee="Ralph",
        body="Complete first task.",
    )
    git(git_repo, "add", ".jri/tasks/todo/first-task.md")
    git(git_repo, "commit", "-m", "add first task")

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())
    assert service.start(max_tasks=1) == 1
    prior_state = service.state_store.load()

    # Simulate a stale doing task on main (as if a worktree iteration crashed)
    write_task(
        git_repo,
        status="doing",
        slug="some-task",
        title="Some task",
        priority=0,
        assignee="Ralph",
        body="Work in progress.",
    )
    git(git_repo, "add", ".jri/tasks/doing/some-task.md")
    git(git_repo, "commit", "-m", "simulate stale doing task")

    stale_attempt = AttemptState(
        number=len(prior_state.attempts) + 1,
        task_slug="some-task",
        branch="ralph",
        started_at=999,
    )
    service.state_store.save(
        State(
            finished_at=prior_state.finished_at,
            session=prior_state.session,
            branch=prior_state.branch,
            process=ProcessState(
                loop_pid=_dead_pid(),
                child_pid=None,
                log_path=None,
                detached=False,
            ),
            active_attempt=stale_attempt,
            attempts=[*prior_state.attempts, stale_attempt],
        )
    )

    (git_repo / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

    service.reset()

    assert git(git_repo, "branch", "--show-current") == "main"
    # Untracked files may remain; reset only discards tracked changes
    assert git(git_repo, "diff", "--name-only") == ""
    assert git(git_repo, "diff", "--cached", "--name-only") == ""

    branches = git(git_repo, "branch", "--format=%(refname:short)").splitlines()
    assert "ralph" not in branches

    state = read_json(git_repo / ".jri" / "state.json")
    assert "process" not in state
    assert "active_attempt" not in state


def test_reset_discards_uncommitted_changes_on_default_branch(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = _run_successful_task(git_repo)

    (git_repo / "untracked-new.txt").write_text("brand new\n", encoding="utf-8")
    (git_repo / "implemented.txt").write_text("modified!\n", encoding="utf-8")

    service.reset()

    assert (git_repo / "implemented.txt").read_text(encoding="utf-8") == "implemented\n"
    assert git(git_repo, "diff", "--name-only") == ""
    assert git(git_repo, "diff", "--cached", "--name-only") == ""


def test_reset_succeeds_when_no_tasks_and_jri0_tag_exists(
    git_repo: Path,
) -> None:
    """When no tasks completed and jri/init tag exists, reset targets jri/init."""
    assert run_cli(["init"], cwd=git_repo) == 0

    # Simulate a partial/failed first task: start creates jri/init tag
    write_task(
        git_repo,
        status="todo",
        slug="failing-task",
        title="Failing task",
        priority=0,
        assignee="Ralph",
        body="This will fail.",
    )
    git(git_repo, "add", ".jri/tasks/todo/failing-task.md")
    git(git_repo, "commit", "-m", "add failing task")

    fail_service = JriService(git_repo, opencode_client=FailedFakeOpenCodeClient())
    assert fail_service.start(max_tasks=1) == 0

    # Now no tasks completed but jri/init exists.
    # Add some extra commits to prove reset discards them.
    (git_repo / "extra-after-fail.txt").write_text("extra\n", encoding="utf-8")
    git(git_repo, "add", "extra-after-fail.txt")
    git(git_repo, "commit", "-m", "extra after failure")

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())
    service.reset()

    # extra file added after the failed iteration should be gone
    assert not (git_repo / "extra-after-fail.txt").exists()
    assert git(git_repo, "branch", "--show-current") == "main"

    state = read_json(git_repo / ".jri" / "state.json")
    assert "process" not in state
    assert "active_attempt" not in state


def test_reset_refuses_when_no_task_tag(
    git_repo: Path,
) -> None:
    """When no task completed and jri/init tag does NOT exist, raise JriError."""
    assert run_cli(["init"], cwd=git_repo) == 0

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())

    with pytest.raises(JriError, match="no task tag found.*jri start"):
        service.reset()


def test_reset_to_jri0_restores_pre_ralph_working_tree(
    git_repo: Path,
) -> None:
    """Reset-to-jri/init removes tracked files added by a failed first task."""
    assert run_cli(["init"], cwd=git_repo) == 0

    write_task(
        git_repo,
        status="todo",
        slug="failing-task",
        title="Failing task",
        priority=0,
        assignee="Ralph",
        body="This will fail.",
    )
    git(git_repo, "add", ".jri/tasks/todo/failing-task.md")
    git(git_repo, "commit", "-m", "add failing task")

    # Start creates jri/init tag, then the failed task runs.
    # The FailedFakeOpenCodeClient doesn't write any files, so add a
    # post-failure commit to prove reset discards it.
    fail_service = JriService(git_repo, opencode_client=FailedFakeOpenCodeClient())
    assert fail_service.start(max_tasks=1) == 0

    # Add a tracked file after the failed iteration
    (git_repo / "added-by-fail.txt").write_text("should be gone\n", encoding="utf-8")
    git(git_repo, "add", "added-by-fail.txt")
    git(git_repo, "commit", "-m", "added by failed iteration")

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())
    service.reset()

    # The extra file should be gone — reset restored to jri/init state
    assert not (git_repo / "added-by-fail.txt").exists()
    # The task file was committed before start() and is part of jri/init
    assert (git_repo / ".jri" / "tasks" / "todo" / "failing-task.md").exists()
    assert git(git_repo, "branch", "--show-current") == "main"


def test_reset_clears_active_attempt(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = _run_successful_task(git_repo)

    prior_state = service.state_store.load()
    fake_attempt = AttemptState(
        number=len(prior_state.attempts) + 1,
        task_slug="stale-task",
        branch="ralph",
        started_at=1234,
    )
    service.state_store.save(
        State(
            finished_at=prior_state.finished_at,
            session=prior_state.session,
            branch=prior_state.branch,
            active_attempt=fake_attempt,
            attempts=[*prior_state.attempts, fake_attempt],
        )
    )

    pre_state = read_json(git_repo / ".jri" / "state.json")
    assert "active_attempt" in pre_state

    service.reset()

    state = read_json(git_repo / ".jri" / "state.json")
    assert "active_attempt" not in state


def test_reset_clears_tracked_process(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = _run_successful_task(git_repo)

    service.state_store.save_process(
        loop_pid=_dead_pid(),
        child_pid=None,
        log_path=git_repo / ".jri" / "logs" / "ralph" / "fake.log",
        detached=True,
    )

    pre_state = read_json(git_repo / ".jri" / "state.json")
    assert "process" in pre_state

    service.reset()

    state = read_json(git_repo / ".jri" / "state.json")
    assert "process" not in state


def test_reset_preserves_attempt_history(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="task-a",
        title="Task A",
        priority=0,
        assignee="Ralph",
        body="Complete task A.",
    )
    git(git_repo, "add", ".jri/tasks/todo/task-a.md")
    git(git_repo, "commit", "-m", "add task-a")

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())
    assert service.start(max_tasks=1) == 1

    write_task(
        git_repo,
        status="todo",
        slug="task-b",
        title="Task B",
        priority=0,
        assignee="Ralph",
        body="This will fail.",
    )
    git(git_repo, "add", ".jri/tasks/todo/task-b.md")
    git(git_repo, "commit", "-m", "add task-b")

    fail_service = JriService(git_repo, opencode_client=FailedFakeOpenCodeClient())
    assert fail_service.start(max_tasks=1) == 0

    service.reset()

    state = read_json(git_repo / ".jri" / "state.json")
    attempts = cast(list[dict[str, object]], state["attempts"])
    assert len(attempts) == 2
    assert attempts[0]["task_slug"] == "task-a"
    assert attempts[0]["outcome"] == "completed"
    assert attempts[1]["task_slug"] == "task-b"
    assert attempts[1]["outcome"] == "failed"


def test_reset_cli_aborts_on_negative_confirmation(git_repo: Path) -> None:
    """Test that reset aborts when user answers 'n' to confirmation prompt."""
    assert run_cli(["init"], cwd=git_repo) == 0
    _run_successful_task(git_repo)

    (git_repo / "extra.txt").write_text("extra content\n", encoding="utf-8")
    git(git_repo, "add", "extra.txt")
    git(git_repo, "commit", "-m", "add extra file")

    # Simulate user entering 'n' for the confirmation prompt
    result = subprocess.run(
        [sys.executable, "-m", "jri", "reset"],
        cwd=git_repo,
        input="n\n",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Reset aborted" in result.stderr
    # extra.txt should still exist because reset was aborted
    assert (git_repo / "extra.txt").exists()


def test_reset_cli_aborts_on_empty_confirmation(git_repo: Path) -> None:
    """Test that reset aborts when user just presses Enter (default N)."""
    assert run_cli(["init"], cwd=git_repo) == 0
    _run_successful_task(git_repo)

    (git_repo / "extra.txt").write_text("extra content\n", encoding="utf-8")
    git(git_repo, "add", "extra.txt")
    git(git_repo, "commit", "-m", "add extra file")

    # Simulate user just pressing Enter
    result = subprocess.run(
        [sys.executable, "-m", "jri", "reset"],
        cwd=git_repo,
        input="\n",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "Reset aborted" in result.stderr
    # extra.txt should still exist because reset was aborted
    assert (git_repo / "extra.txt").exists()


def test_reset_cli_force_skips_confirmation(git_repo: Path) -> None:
    """Test that --force flag skips the confirmation prompt."""
    assert run_cli(["init"], cwd=git_repo) == 0
    _run_successful_task(git_repo)

    (git_repo / "extra.txt").write_text("extra content\n", encoding="utf-8")
    git(git_repo, "add", "extra.txt")
    git(git_repo, "commit", "-m", "add extra file")

    # Use --force flag, should not prompt
    result = subprocess_module.run(
        [sys.executable, "-m", "jri", "reset", "--force"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    # extra.txt should be gone because reset proceeded
    assert not (git_repo / "extra.txt").exists()


def test_reset_cli_prompt_includes_target_tag(git_repo: Path) -> None:
    """Test that the confirmation prompt includes the target tag name."""
    assert run_cli(["init"], cwd=git_repo) == 0
    _run_successful_task(git_repo)

    # Simulate user entering 'n' to see the prompt
    result = subprocess_module.run(
        [sys.executable, "-m", "jri", "reset"],
        cwd=git_repo,
        input="n\n",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    # The prompt should mention the end tag for the completed task
    assert "jri/end/implement-file" in result.stdout


def test_reset_cli_prompt_shows_uncommitted_changes(git_repo: Path) -> None:
    """Test that the confirmation prompt mentions uncommitted changes."""
    assert run_cli(["init"], cwd=git_repo) == 0
    _run_successful_task(git_repo)

    # Create an uncommitted change
    (git_repo / "uncommitted.txt").write_text("uncommitted\n", encoding="utf-8")

    # Simulate user entering 'n' to see the prompt
    result = subprocess_module.run(
        [sys.executable, "-m", "jri", "reset"],
        cwd=git_repo,
        input="n\n",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    # The prompt should mention uncommitted changes
    assert "Uncommitted changes will be discarded" in result.stdout


def test_reset_cli_prompt_shows_ralph_branch(git_repo: Path) -> None:
    """Test that the confirmation prompt mentions the ralph branch to be deleted."""
    assert run_cli(["init"], cwd=git_repo) == 0
    _run_successful_task(git_repo)

    # Simulate user entering 'n' to see the prompt
    result = subprocess_module.run(
        [sys.executable, "-m", "jri", "reset"],
        cwd=git_repo,
        input="n\n",
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    # The prompt should mention the ralph branch and worktree
    assert "The ralph branch and worktree will be deleted." in result.stdout


def test_reset_cli_help_shows_force_flag(git_repo: Path) -> None:
    """Test that --help documents the --force flag."""
    result = subprocess_module.run(
        [sys.executable, "-m", "jri", "reset", "--help"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--force" in result.stdout
    assert "-f" in result.stdout
