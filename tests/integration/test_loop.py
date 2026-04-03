import os
import signal
import subprocess
from pathlib import Path
from typing import cast

import pytest

from jri.core.errors import JriError
from jri.core.models import OpenCodeRunResult
from jri.core.opencode import OpenCodeClient
from jri.core.service import JriService
from tests.conftest import run_cli
from tests.helpers import git, read_json, write_task


class SuccessfulFakeOpenCodeClient(OpenCodeClient):
    def __init__(self) -> None:
        super().__init__(model=None)
        self.calls: list[tuple[str, Path]] = []
        self.models_used: list[str | None] = []

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        on_start: object | None = None,
    ) -> OpenCodeRunResult:
        self.calls.append((prompt, log_path))
        self.models_used.append(self.model)
        (root / "implemented.txt").write_text("implemented\n", encoding="utf-8")
        log_path.write_text("fake run\n", encoding="utf-8")
        return OpenCodeRunResult(
            returncode=0, session_id="ses_fake", outcome="completed"
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake"}\n', encoding="utf-8")


class BlockedFakeOpenCodeClient(OpenCodeClient):
    """Simulates Ralph hitting a blocker and outputting JRI:BLOCKED signal."""

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
    ) -> OpenCodeRunResult:
        self.calls.append((prompt, log_path))
        log_path.write_text("fake blocked run\n", encoding="utf-8")
        return OpenCodeRunResult(
            returncode=0, session_id="ses_blocked", outcome="blocked"
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake_blocked"}\n', encoding="utf-8")


class BlockedThenSuccessfulFakeOpenCodeClient(OpenCodeClient):
    """Returns blocked for the first call, successful for the second."""

    def __init__(self) -> None:
        super().__init__(model=None)
        self.calls: list[tuple[str, Path]] = []
        self._call_count = 0

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        on_start: object | None = None,
    ) -> OpenCodeRunResult:
        self.calls.append((prompt, log_path))
        self._call_count += 1
        log_path.write_text(f"fake run #{self._call_count}\n", encoding="utf-8")
        if self._call_count == 1:
            return OpenCodeRunResult(
                returncode=0, session_id="ses_blocked", outcome="blocked"
            )
        (root / "implemented.txt").write_text("implemented\n", encoding="utf-8")
        return OpenCodeRunResult(returncode=0, session_id="ses_ok", outcome="completed")

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake"}\n', encoding="utf-8")


class MissingDoingTaskOpenCodeClient(SuccessfulFakeOpenCodeClient):
    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        on_start: object | None = None,
    ) -> OpenCodeRunResult:
        (root / ".jri" / "tasks" / "doing" / "implement-file.md").unlink()
        return super().run_ralph_task(
            root=root,
            prompt=prompt,
            log_path=log_path,
            on_start=on_start,
        )


def test_start_uses_explicit_model_override(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    client = SuccessfulFakeOpenCodeClient()
    service = JriService(git_repo, opencode_client=client)

    completed = service.start(iterations=1, model="opencode/qwen3.6-plus-free")

    assert completed == 1
    assert client.models_used == ["opencode/qwen3.6-plus-free"]
    assert client.model is None


def test_start_completes_single_iteration(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())

    completed = service.start(iterations=1)

    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "implement-file.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "todo" / "implement-file.md").exists()
    assert (git_repo / "implemented.txt").read_text(encoding="utf-8") == "implemented\n"
    assert git(git_repo, "branch", "--show-current") == "main"
    assert (
        "ralph/1/implement-file"
        in git(git_repo, "branch", "--format=%(refname:short)").splitlines()
    )
    assert "jri/1" in git(git_repo, "tag").splitlines()
    iteration = read_json(git_repo / ".jri" / "state.json")["iteration"]
    iteration_payload = cast(dict[str, object], iteration)
    assert iteration_payload["number"] == 1
    assert git(git_repo, "status", "--short") == ""


def test_start_passes_doing_task_path_to_ralph(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    client = SuccessfulFakeOpenCodeClient()
    service = JriService(git_repo, opencode_client=client)

    assert service.start(iterations=1) == 1
    assert len(client.calls) == 1
    assert (
        client.calls[0][0]
        == "Solve `.jri/tasks/doing/implement-file.md`. Commit frequently."
    )


def test_start_fails_cleanly_when_doing_task_disappears(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")

    service = JriService(git_repo, opencode_client=MissingDoingTaskOpenCodeClient())

    with pytest.raises(JriError, match="disappeared during Ralph run"):
        service.start(iterations=1)


def test_start_refuses_when_task_is_already_doing(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="existing",
        title="Existing",
        priority=1,
        assignee="Ralph",
        body="Already running.",
    )

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())

    try:
        service.start(iterations=1)
    except Exception as error:
        assert "already in progress" in str(error)
    else:
        raise AssertionError("expected the loop to reject an existing doing task")


def test_stop_creates_stop_signal(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())

    service.stop("maintenance window")

    assert (git_repo / ".jri" / "signals" / "stop").read_text(
        encoding="utf-8"
    ) == "maintenance window\n"


def test_reset_returns_repo_to_last_successful_iteration(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "commit", "-m", "add task")
    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())
    assert service.start(iterations=1) == 1
    service.state_store.save_session("ses_interrogation")

    (git_repo / "extra.txt").write_text("later\n", encoding="utf-8")
    git(git_repo, "add", "extra.txt")
    git(git_repo, "commit", "-m", "extra")

    service.reset()

    assert not (git_repo / "extra.txt").exists()
    iteration = read_json(git_repo / ".jri" / "state.json")["iteration"]
    iteration_payload = cast(dict[str, object], iteration)
    assert iteration_payload["number"] == 1
    assert read_json(git_repo / ".jri" / "state.json")["session"] == "ses_interrogation"
    assert "finished_at" in iteration_payload


def test_halt_terminates_tracked_process(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    sleeper = subprocess.Popen(["sleep", "30"])
    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())
    service.state_store.save_process(
        loop_pid=sleeper.pid, child_pid=None, log_path=None, detached=True
    )

    try:
        service.halt()
        sleeper.wait(timeout=5)
    finally:
        if sleeper.poll() is None:
            os.kill(sleeper.pid, signal.SIGTERM)

    assert sleeper.returncode is not None


def test_blocked_task_moves_back_to_todo(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="blocked-task",
        title="Blocked task",
        priority=0,
        assignee="Ralph",
        body="This will be blocked.",
    )
    git(git_repo, "add", ".jri/tasks/todo/blocked-task.md")
    git(git_repo, "commit", "-m", "add blocked task")

    service = JriService(git_repo, opencode_client=BlockedFakeOpenCodeClient())

    completed = service.start(iterations=1)

    assert completed == 0
    assert (git_repo / ".jri" / "tasks" / "todo" / "blocked-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "blocked-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "blocked-task.md").exists()
    assert git(git_repo, "branch", "--show-current") == "main"
    # The feature branch should be deleted
    branches = git(git_repo, "branch", "--format=%(refname:short)").splitlines()
    assert not any("ralph/" in b for b in branches)


def test_blocked_then_successful_completes_one(git_repo: Path) -> None:
    """Two tasks: first is blocked, loop continues and completes the second."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="task-a",
        title="Task A (blocked)",
        priority=0,
        assignee="Ralph",
        body="Will be blocked.",
    )
    write_task(
        git_repo,
        status="todo",
        slug="task-b",
        title="Task B (success)",
        priority=1,
        assignee="Ralph",
        body="Will succeed.",
    )
    git(git_repo, "add", ".jri/tasks/todo/")
    git(git_repo, "commit", "-m", "add two tasks")

    client = BlockedThenSuccessfulFakeOpenCodeClient()
    service = JriService(git_repo, opencode_client=client)

    completed = service.start(iterations=2)

    assert completed == 1
    # Blocked task is back in todo
    assert (git_repo / ".jri" / "tasks" / "todo" / "task-a.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "task-a.md").exists()
    # Successful task is in done
    assert (git_repo / ".jri" / "tasks" / "done" / "task-b.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "todo" / "task-b.md").exists()
    # Only the successful branch remains
    branches = git(git_repo, "branch", "--format=%(refname:short)").splitlines()
    assert not any("task-a" in b for b in branches)
    assert git(git_repo, "branch", "--show-current") == "main"


class MakeCheckFailsFakeOpenCodeClient(OpenCodeClient):
    """Successful Ralph run, but the project has a failing make check."""

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
    ) -> OpenCodeRunResult:
        self.calls.append((prompt, log_path))
        (root / "implemented.txt").write_text("implemented\n", encoding="utf-8")
        log_path.write_text("fake run\n", encoding="utf-8")
        return OpenCodeRunResult(
            returncode=0, session_id="ses_fake", outcome="completed"
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake"}\n', encoding="utf-8")


class UnknownOutcomeFakeOpenCodeClient(OpenCodeClient):
    """Simulates Ralph returning an unknown outcome (no JRI markers)."""

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
    ) -> OpenCodeRunResult:
        self.calls.append((prompt, log_path))
        log_path.write_text("fake unknown run\n", encoding="utf-8")
        return OpenCodeRunResult(
            returncode=0, session_id="ses_unknown", outcome="unknown"
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake_unknown"}\n', encoding="utf-8")


def test_unknown_outcome_triggers_recovery(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="mystery-task",
        title="Mystery task",
        priority=0,
        assignee="Ralph",
        body="This will have an unknown outcome.",
    )
    git(git_repo, "add", ".jri/tasks/todo/mystery-task.md")
    git(git_repo, "commit", "-m", "add mystery task")

    service = JriService(git_repo, opencode_client=UnknownOutcomeFakeOpenCodeClient())

    completed = service.start(iterations=1)

    assert completed == 0
    assert (git_repo / ".jri" / "tasks" / "todo" / "mystery-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "mystery-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "mystery-task.md").exists()
    assert git(git_repo, "branch", "--show-current") == "main"
    branches = git(git_repo, "branch", "--format=%(refname:short)").splitlines()
    assert not any("ralph/" in b for b in branches)


def test_make_check_runs_after_completion(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    # Create a Makefile with a passing check target
    (git_repo / "Makefile").write_text("check:\n\t@echo ok\n", encoding="utf-8")
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "add", "Makefile")
    git(git_repo, "commit", "-m", "add task and makefile")

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())

    completed = service.start(iterations=1)

    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "implement-file.md").exists()
    assert git(git_repo, "branch", "--show-current") == "main"


def test_failing_make_check_triggers_recovery(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    # Create a Makefile with a failing check target
    (git_repo / "Makefile").write_text("check:\n\texit 1\n", encoding="utf-8")
    write_task(
        git_repo,
        status="todo",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/todo/implement-file.md")
    git(git_repo, "add", "Makefile")
    git(git_repo, "commit", "-m", "add task and makefile")

    service = JriService(git_repo, opencode_client=MakeCheckFailsFakeOpenCodeClient())

    completed = service.start(iterations=1)

    assert completed == 0
    # Task should be back in todo after recovery
    assert (git_repo / ".jri" / "tasks" / "todo" / "implement-file.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "implement-file.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "implement-file.md").exists()
    assert git(git_repo, "branch", "--show-current") == "main"
    # The feature branch should be deleted
    branches = git(git_repo, "branch", "--format=%(refname:short)").splitlines()
    assert not any("ralph/" in b for b in branches)
