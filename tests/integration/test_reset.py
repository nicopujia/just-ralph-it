import subprocess
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


def _dead_pid() -> int:
    process = subprocess.Popen(["sleep", "0"])
    process.wait(timeout=5)
    return process.pid


def _run_successful_iteration(repo: Path) -> JriService:
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
    assert service.start(iterations=1) == 1
    return service


def test_reset_after_successful_iteration(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = _run_successful_iteration(git_repo)
    service.state_store.save_session("ses_reset_test")

    (git_repo / "extra.txt").write_text("extra content\n", encoding="utf-8")
    git(git_repo, "add", "extra.txt")
    git(git_repo, "commit", "-m", "add extra file")

    service.reset()

    assert not (git_repo / "extra.txt").exists()
    assert (git_repo / "implemented.txt").read_text(encoding="utf-8") == "implemented\n"
    assert git(git_repo, "branch", "--show-current") == "main"

    state = read_json(git_repo / ".jri" / "state.json")
    iteration = cast(dict[str, object], state["iteration"])
    assert iteration["number"] == 1
    assert "finished_at" in iteration
    assert state["session"] == "ses_reset_test"
    assert "process" not in state
    assert "active_attempt" not in state


def test_reset_after_failed_iteration(git_repo: Path) -> None:
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
    assert service.start(iterations=1) == 1

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
    assert fail_service.start(iterations=1) == 0

    (git_repo / "extra-after-fail.txt").write_text("extra\n", encoding="utf-8")
    git(git_repo, "add", "extra-after-fail.txt")
    git(git_repo, "commit", "-m", "extra after failure")

    service.reset()

    assert not (git_repo / "extra-after-fail.txt").exists()
    assert (git_repo / "implemented.txt").read_text(encoding="utf-8") == "implemented\n"
    assert git(git_repo, "branch", "--show-current") == "main"

    state = read_json(git_repo / ".jri" / "state.json")
    iteration = cast(dict[str, object], state["iteration"])
    assert iteration["number"] == 1


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
    assert service.start(iterations=1) == 1
    prior_state = service.state_store.load()

    git(git_repo, "checkout", "-b", "ralph/2/some-task")
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
    git(git_repo, "commit", "-m", "start some-task on feature branch")

    stale_attempt = AttemptState(
        number=len(prior_state.attempts) + 1,
        task_slug="some-task",
        iteration_number=2,
        branch="ralph/2/some-task",
        started_at=999,
    )
    service.state_store.save(
        State(
            iteration_number=prior_state.iteration_number,
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
    assert "ralph/2/some-task" not in branches

    state = read_json(git_repo / ".jri" / "state.json")
    iteration = cast(dict[str, object], state["iteration"])
    assert iteration["number"] == 1
    assert "process" not in state
    assert "active_attempt" not in state


def test_reset_discards_uncommitted_changes_on_default_branch(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = _run_successful_iteration(git_repo)

    (git_repo / "untracked-new.txt").write_text("brand new\n", encoding="utf-8")
    (git_repo / "implemented.txt").write_text("modified!\n", encoding="utf-8")

    service.reset()

    assert (git_repo / "implemented.txt").read_text(encoding="utf-8") == "implemented\n"
    assert git(git_repo, "diff", "--name-only") == ""
    assert git(git_repo, "diff", "--cached", "--name-only") == ""

    state = read_json(git_repo / ".jri" / "state.json")
    iteration = cast(dict[str, object], state["iteration"])
    assert iteration["number"] == 1


def test_reset_refuses_when_no_successful_iteration(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())

    with pytest.raises(JriError, match="no successful iteration"):
        service.reset()


def test_reset_clears_active_attempt(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = _run_successful_iteration(git_repo)

    prior_state = service.state_store.load()
    fake_attempt = AttemptState(
        number=len(prior_state.attempts) + 1,
        task_slug="stale-task",
        iteration_number=2,
        branch="ralph/2/stale-task",
        started_at=1234,
    )
    service.state_store.save(
        State(
            iteration_number=prior_state.iteration_number,
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
    service = _run_successful_iteration(git_repo)

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
    assert service.start(iterations=1) == 1

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
    assert fail_service.start(iterations=1) == 0

    service.reset()

    state = read_json(git_repo / ".jri" / "state.json")
    attempts = cast(list[dict[str, object]], state["attempts"])
    assert len(attempts) == 2
    assert attempts[0]["task_slug"] == "task-a"
    assert attempts[0]["outcome"] == "completed"
    assert attempts[1]["task_slug"] == "task-b"
    assert attempts[1]["outcome"] == "failed"
