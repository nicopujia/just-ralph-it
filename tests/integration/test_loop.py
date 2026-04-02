from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import cast

from jri.models import OpenCodeRunResult
from jri.opencode import OpenCodeClient
from jri.service import JriService
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
    ) -> OpenCodeRunResult:
        self.calls.append((prompt, log_path))
        (root / "implemented.txt").write_text("implemented\n", encoding="utf-8")
        log_path.write_text("fake run\n", encoding="utf-8")
        return OpenCodeRunResult(returncode=0, session_id="ses_fake")

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake"}\n', encoding="utf-8")


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

    (git_repo / "extra.txt").write_text("later\n", encoding="utf-8")
    git(git_repo, "add", "extra.txt")
    git(git_repo, "commit", "-m", "extra")

    service.reset()

    assert not (git_repo / "extra.txt").exists()
    iteration = read_json(git_repo / ".jri" / "state.json")["iteration"]
    iteration_payload = cast(dict[str, object], iteration)
    assert iteration_payload["number"] == 1


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
