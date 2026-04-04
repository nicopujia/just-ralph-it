import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from jri.core.errors import JriError
from jri.core.models import AttemptState, OpenCodeRunResult, State
from jri.core.opencode import OpenCodeClient
from jri.core.service import JriService
from jri.core.tasks import list_tasks, parse_task_file
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


class NeedsHumanFakeOpenCodeClient(OpenCodeClient):
    """Simulates Ralph resolving the task as needs human."""

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
        log_path.write_text("fake needs-human run\n", encoding="utf-8")
        return OpenCodeRunResult(
            returncode=0,
            session_id="ses_needs_human",
            outcome="needs human",
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake_needs_human"}\n', encoding="utf-8")


class NeedsHumanThenSuccessfulFakeOpenCodeClient(OpenCodeClient):
    """Returns needs human for the first call, successful for the second."""

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
                returncode=0,
                session_id="ses_needs_human",
                outcome="needs human",
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


class MutatingDoingTaskOpenCodeClient(SuccessfulFakeOpenCodeClient):
    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        on_start: object | None = None,
    ) -> OpenCodeRunResult:
        doing_path = root / ".jri" / "tasks" / "doing" / "implement-file.md"
        doing_path.write_text(
            doing_path.read_text(encoding="utf-8") + "\nMutated in place.\n",
            encoding="utf-8",
        )
        return super().run_ralph_task(
            root=root,
            prompt=prompt,
            log_path=log_path,
            on_start=on_start,
        )


class CommittedMutatingDoingTaskOpenCodeClient(SuccessfulFakeOpenCodeClient):
    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        on_start: object | None = None,
    ) -> OpenCodeRunResult:
        doing_path = root / ".jri" / "tasks" / "doing" / "implement-file.md"
        doing_path.write_text(
            doing_path.read_text(encoding="utf-8") + "\nCommitted mutation.\n",
            encoding="utf-8",
        )
        git(root, "add", ".jri/tasks/doing/implement-file.md")
        git(root, "commit", "-m", "mutate task in place")
        return super().run_ralph_task(
            root=root,
            prompt=prompt,
            log_path=log_path,
            on_start=on_start,
        )


class FollowUpDraftOpenCodeClient(SuccessfulFakeOpenCodeClient):
    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        on_start: object | None = None,
    ) -> OpenCodeRunResult:
        write_task(
            root,
            status="draft",
            slug="follow-up-fix",
            title="Follow up fix",
            priority=1,
            assignee="Ralph",
            body="Capture the additive follow-up work.\n",
            acceptance_criteria=["Follow-up task is triaged"],
        )
        return super().run_ralph_task(
            root=root,
            prompt=prompt,
            log_path=log_path,
            on_start=on_start,
        )


class FakeDetachedProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid


def _dead_pid() -> int:
    process = subprocess.Popen(["sleep", "0"])
    process.wait(timeout=5)
    return process.pid


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
    tags = git(git_repo, "tag").splitlines()
    assert "jri/0" in tags
    assert "jri/1" in tags
    iteration = read_json(git_repo / ".jri" / "state.json")["iteration"]
    iteration_payload = cast(dict[str, object], iteration)
    assert iteration_payload["number"] == 1
    attempts = cast(
        list[dict[str, object]],
        read_json(git_repo / ".jri" / "state.json")["attempts"],
    )
    assert len(attempts) == 1
    assert attempts[0]["number"] == 1
    assert attempts[0]["task_slug"] == "implement-file"
    assert attempts[0]["iteration_number"] == 1
    assert attempts[0]["outcome"] == "completed"
    assert attempts[0]["session_id"] == "ses_fake"
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


def test_start_rejects_in_place_mutation_of_doing_task(git_repo: Path) -> None:
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

    service = JriService(git_repo, opencode_client=MutatingDoingTaskOpenCodeClient())

    with pytest.raises(JriError, match="modified in place"):
        service.start(iterations=1)


def test_start_rejects_committed_in_place_mutation_of_doing_task(
    git_repo: Path,
) -> None:
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

    service = JriService(
        git_repo,
        opencode_client=CommittedMutatingDoingTaskOpenCodeClient(),
    )

    with pytest.raises(JriError, match="modified in place"):
        service.start(iterations=1)


def test_start_allows_additive_follow_up_draft_tasks(git_repo: Path) -> None:
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

    service = JriService(git_repo, opencode_client=FollowUpDraftOpenCodeClient())

    completed = service.start(iterations=1)

    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "implement-file.md").exists()
    follow_up = parse_task_file(
        git_repo / ".jri" / "tasks" / "draft" / "follow-up-fix.md"
    )
    assert follow_up.metadata.title == "Follow up fix"
    assert "additive follow-up" in follow_up.body


def test_start_refuses_when_tracked_process_is_still_alive(git_repo: Path) -> None:
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
    sleeper = subprocess.Popen(["sleep", "30"])

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())
    service.state_store.save_process(
        loop_pid=sleeper.pid,
        child_pid=None,
        log_path=None,
        detached=False,
    )

    try:
        with pytest.raises(JriError, match="already running"):
            service.start(iterations=1)
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_start_recovers_clean_foreground_interruption(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/doing/implement-file.md")
    git(git_repo, "commit", "-m", "seed interrupted task")

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())

    completed = service.start(iterations=1)

    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "implement-file.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "implement-file.md").exists()
    recovery_log = (git_repo / ".jri" / "logs" / "recovery.log").read_text(
        encoding="utf-8"
    )
    assert "mode=foreground" in recovery_log
    assert "task=implement-file" in recovery_log
    assert "reason=no-tracked-process" in recovery_log
    history = git(git_repo, "log", "--oneline", "--decorate=short", "-5")
    assert "jri: recover implement-file after stale run" in history


def test_start_records_retry_attempt_after_interrupted_run(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
    )
    git(git_repo, "add", ".jri/tasks/doing/implement-file.md")
    git(git_repo, "commit", "-m", "seed interrupted attempt")

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())
    interrupted_attempt = AttemptState(
        number=1,
        task_slug="implement-file",
        iteration_number=1,
        branch="ralph/1/implement-file",
        started_at=123,
        log_path=".jri/logs/ralph/1-interrupted.log",
    )
    service.state_store.save(
        State(
            started_at=123,
            branch="main",
            active_attempt=interrupted_attempt,
            attempts=[interrupted_attempt],
        )
    )

    completed = service.start(iterations=1)

    assert completed == 1
    attempts = cast(
        list[dict[str, object]],
        read_json(git_repo / ".jri" / "state.json")["attempts"],
    )
    assert [attempt["number"] for attempt in attempts] == [1, 2]
    assert [attempt["task_slug"] for attempt in attempts] == [
        "implement-file",
        "implement-file",
    ]
    assert attempts[0]["outcome"] == "interrupted"
    assert attempts[1]["outcome"] == "completed"


def test_start_recovers_stale_foreground_process(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/doing/implement-file.md")
    git(git_repo, "commit", "-m", "seed stale process task")
    git(git_repo, "checkout", "-b", "ralph/1/implement-file")
    git(git_repo, "checkout", "main")

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())
    service.state_store.save_process(
        loop_pid=_dead_pid(),
        child_pid=None,
        log_path=git_repo / ".jri" / "logs" / "ralph" / "stale.log",
        detached=False,
    )

    completed = service.start(iterations=1)

    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "implement-file.md").exists()
    recovery_log = (git_repo / ".jri" / "logs" / "recovery.log").read_text(
        encoding="utf-8"
    )
    assert "mode=foreground" in recovery_log
    assert "task=implement-file" in recovery_log
    assert "reason=dead-tracked-process" in recovery_log
    history = git(git_repo, "log", "--oneline", "--decorate=short", "-6")
    assert "jri: recover implement-file after stale run" in history


def test_start_clears_stale_process_metadata_without_doing_task(git_repo: Path) -> None:
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
    git(git_repo, "commit", "-m", "seed stale process metadata")

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())
    service.state_store.save_process(
        loop_pid=_dead_pid(),
        child_pid=None,
        log_path=git_repo / ".jri" / "logs" / "ralph" / "orphaned.log",
        detached=False,
    )

    assert service.start(iterations=1) == 1
    recovery_log = (git_repo / ".jri" / "logs" / "recovery.log").read_text(
        encoding="utf-8"
    )
    assert "task=-" in recovery_log
    assert "reason=dead-tracked-process" in recovery_log


def test_start_recovers_clean_detached_interruption(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/doing/implement-file.md")
    git(git_repo, "commit", "-m", "seed clean detached interruption")

    popen_calls: list[list[str]] = []
    original_popen = cast(Any, subprocess.Popen)
    expected_command = [sys.executable, "-m", "jri", "start", "-n", "1"]

    def fake_popen(*args: object, **kwargs: object) -> object:
        command = cast(list[str], args[0])
        if command != expected_command:
            return original_popen(*args, **kwargs)
        popen_calls.append(command)
        assert kwargs["cwd"] == git_repo
        assert kwargs["start_new_session"] is True
        return FakeDetachedProcess(424242)

    monkeypatch.setattr("jri.core.service.subprocess.Popen", fake_popen)
    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())

    assert service.start(iterations=1, detached=True) == 0
    assert popen_calls == [expected_command]
    assert (git_repo / ".jri" / "tasks" / "todo" / "implement-file.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "implement-file.md").exists()
    process = cast(
        dict[str, object], read_json(git_repo / ".jri" / "state.json")["process"]
    )
    assert process["loop_pid"] == 424242
    assert process["detached"] is True
    recovery_log = (git_repo / ".jri" / "logs" / "recovery.log").read_text(
        encoding="utf-8"
    )
    assert "mode=detached" in recovery_log
    assert "reason=no-tracked-process" in recovery_log


def test_start_recovers_stale_detached_process(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="implement-file",
        title="Implement file",
        priority=0,
        assignee="Ralph",
        body="Create implemented.txt with the text implemented.",
        acceptance_criteria=["implemented.txt exists"],
    )
    git(git_repo, "add", ".jri/tasks/doing/implement-file.md")
    git(git_repo, "commit", "-m", "seed stale detached process")

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())
    service.state_store.save_process(
        loop_pid=_dead_pid(),
        child_pid=None,
        log_path=git_repo / ".jri" / "logs" / "ralph" / "stale-detached.log",
        detached=True,
    )

    original_popen = cast(Any, subprocess.Popen)
    expected_command = [sys.executable, "-m", "jri", "start", "-n", "1"]

    def fake_popen(*args: object, **kwargs: object) -> object:
        command = cast(list[str], args[0])
        if command != expected_command:
            return original_popen(*args, **kwargs)
        assert kwargs["cwd"] == git_repo
        assert kwargs["start_new_session"] is True
        return FakeDetachedProcess(313131)

    monkeypatch.setattr("jri.core.service.subprocess.Popen", fake_popen)

    assert service.start(iterations=1, detached=True) == 0
    assert (git_repo / ".jri" / "tasks" / "todo" / "implement-file.md").exists()
    process = cast(
        dict[str, object], read_json(git_repo / ".jri" / "state.json")["process"]
    )
    assert process["loop_pid"] == 313131
    assert process["detached"] is True
    recovery_log = (git_repo / ".jri" / "logs" / "recovery.log").read_text(
        encoding="utf-8"
    )
    assert "mode=detached" in recovery_log
    assert "reason=dead-tracked-process" in recovery_log


def test_start_retries_after_interrupted_completion_without_rerunning_task(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
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
        priority=1,
        assignee="Ralph",
        body="Complete task B.",
    )
    git(git_repo, "add", ".jri/tasks/todo")
    git(git_repo, "commit", "-m", "add retry tasks")

    first_client = SuccessfulFakeOpenCodeClient()
    first_service = JriService(git_repo, opencode_client=first_client)

    def interrupted_mark_iteration_finished(
        *, iteration_number: int, finished_at: int
    ) -> None:
        raise KeyboardInterrupt("simulated interruption during completion")

    monkeypatch.setattr(
        first_service.state_store,
        "mark_iteration_finished",
        interrupted_mark_iteration_finished,
    )

    with pytest.raises(KeyboardInterrupt, match="simulated interruption"):
        first_service.start(iterations=1)

    interrupted_state = read_json(git_repo / ".jri" / "state.json")
    interrupted_iteration = cast(dict[str, object], interrupted_state["iteration"])
    interrupted_attempt = cast(dict[str, object], interrupted_state["active_attempt"])
    assert interrupted_iteration["number"] == 0
    assert interrupted_attempt["task_slug"] == "task-a"
    assert interrupted_attempt["outcome"] == "completed"
    assert (git_repo / ".jri" / "tasks" / "done" / "task-a.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "task-a.md").exists()
    assert (git_repo / ".jri" / "tasks" / "todo" / "task-b.md").exists()
    assert git(git_repo, "branch", "--show-current") == "main"
    assert "jri/1" in git(git_repo, "tag").splitlines()

    retry_client = SuccessfulFakeOpenCodeClient()
    retry_service = JriService(git_repo, opencode_client=retry_client)

    assert retry_service.start(iterations=1) == 1
    assert len(retry_client.calls) == 1
    assert "task-b" in retry_client.calls[0][0]

    final_state = read_json(git_repo / ".jri" / "state.json")
    final_iteration = cast(dict[str, object], final_state["iteration"])
    attempts = cast(list[dict[str, object]], final_state["attempts"])
    assert final_iteration["number"] == 2
    assert final_state.get("active_attempt") is None
    assert [attempt["task_slug"] for attempt in attempts] == ["task-a", "task-b"]
    assert attempts[0]["outcome"] == "completed"
    assert attempts[1]["outcome"] == "completed"
    assert (git_repo / ".jri" / "tasks" / "done" / "task-b.md").exists()


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


def test_needs_human_generates_human_followup_and_blocks_original_task(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="needs-human-task",
        title="Needs human task",
        priority=0,
        assignee="Ralph",
        body="This will need human help.",
    )
    git(git_repo, "add", ".jri/tasks/todo/needs-human-task.md")
    git(git_repo, "commit", "-m", "add needs human task")

    service = JriService(git_repo, opencode_client=NeedsHumanFakeOpenCodeClient())

    completed = service.start(iterations=1)

    todo_tasks = list_tasks(git_repo / ".jri" / "tasks" / "todo")
    original_task = parse_task_file(
        git_repo / ".jri" / "tasks" / "todo" / "needs-human-task.md"
    )
    human_tasks = [task for task in todo_tasks if task.metadata.assignee == "Human"]

    assert completed == 0
    assert len(human_tasks) == 1
    human_task = human_tasks[0]
    assert human_task.metadata.priority == 0
    assert original_task.metadata.depends_on == [human_task.slug]
    assert "needs-human-task" in human_task.body
    assert ".jri/tasks/todo/needs-human-task.md" in human_task.body
    assert ".jri/logs/ralph/" in human_task.body
    assert "ses_needs_human" in human_task.body
    assert ".jri/logs/external/opencode/ses_needs_human.json" in human_task.body
    assert (
        git_repo / ".jri" / "logs" / "external" / "opencode" / "ses_needs_human.json"
    ).exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "needs-human-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "needs-human-task.md").exists()
    assert git(git_repo, "branch", "--show-current") == "main"
    tags = git(git_repo, "tag").splitlines()
    assert "jri/0" in tags
    assert "jri/1" not in tags
    # The feature branch should be deleted
    branches = git(git_repo, "branch", "--format=%(refname:short)").splitlines()
    assert not any("ralph/" in b for b in branches)


def test_needs_human_block_is_durable_across_runs(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="needs-human-task",
        title="Needs human task",
        priority=0,
        assignee="Ralph",
        body="This will need human help.",
    )
    git(git_repo, "add", ".jri/tasks/todo/needs-human-task.md")
    git(git_repo, "commit", "-m", "add needs human task")

    service = JriService(git_repo, opencode_client=NeedsHumanFakeOpenCodeClient())

    assert service.start(iterations=1) == 0

    retry_client = SuccessfulFakeOpenCodeClient()
    retry_service = JriService(git_repo, opencode_client=retry_client)

    assert retry_service.start(iterations=1) == 0
    assert retry_client.calls == []


def test_needs_human_then_successful_completes_one(git_repo: Path) -> None:
    """Two tasks: first needs human, loop continues and completes the second."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="task-a",
        title="Task A (needs human)",
        priority=0,
        assignee="Ralph",
        body="Will need human help.",
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

    client = NeedsHumanThenSuccessfulFakeOpenCodeClient()
    service = JriService(git_repo, opencode_client=client)

    completed = service.start(iterations=2)

    todo_tasks = list_tasks(git_repo / ".jri" / "tasks" / "todo")
    task_a = parse_task_file(git_repo / ".jri" / "tasks" / "todo" / "task-a.md")
    human_tasks = [task for task in todo_tasks if task.metadata.assignee == "Human"]

    assert completed == 1
    assert len(human_tasks) == 1
    assert task_a.metadata.depends_on == [human_tasks[0].slug]
    # Needs-human task is back in todo, now blocked on a generated Human task
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


class FailedFakeOpenCodeClient(OpenCodeClient):
    """Simulates Ralph explicitly returning a failed outcome."""

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
        log_path.write_text("fake failed run\n", encoding="utf-8")
        return OpenCodeRunResult(
            returncode=0, session_id="ses_failed", outcome="failed"
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text('{"session": "fake_failed"}\n', encoding="utf-8")


def test_failed_outcome_triggers_recovery(git_repo: Path) -> None:
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

    service = JriService(git_repo, opencode_client=FailedFakeOpenCodeClient())

    completed = service.start(iterations=1)

    assert completed == 0
    assert (git_repo / ".jri" / "tasks" / "todo" / "failing-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "failing-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "failing-task.md").exists()
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


def test_failed_task_is_retried_after_first_failure(git_repo: Path) -> None:
    """A task that fails once is retried on the next loop invocation."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="failing-task",
        title="Failing task",
        priority=0,
        assignee="Ralph",
        body="This will fail once.",
    )
    git(git_repo, "add", ".jri/tasks/todo/failing-task.md")
    git(git_repo, "commit", "-m", "add failing task")

    # First run fails
    fail_client = FailedFakeOpenCodeClient()
    fail_service = JriService(git_repo, opencode_client=fail_client)
    completed = fail_service.start(iterations=1)

    assert completed == 0
    assert (git_repo / ".jri" / "tasks" / "todo" / "failing-task.md").exists()
    # One failed attempt recorded
    state = read_json(git_repo / ".jri" / "state.json")
    attempts = cast(list[dict[str, object]], state["attempts"])
    failed_for_task = [a for a in attempts if a["task_slug"] == "failing-task"]
    assert len(failed_for_task) == 1
    assert failed_for_task[0]["outcome"] == "failed"

    # Second run succeeds (task is retried)
    success_client = SuccessfulFakeOpenCodeClient()
    success_service = JriService(git_repo, opencode_client=success_client)
    completed = success_service.start(iterations=1)

    assert completed == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "failing-task.md").exists()
    assert len(success_client.calls) == 1
    assert "failing-task" in success_client.calls[0][0]


def test_failed_task_is_retried_up_to_three_times(git_repo: Path) -> None:
    """A task that fails twice is still retryable on the next start."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="failing-task",
        title="Failing task",
        priority=0,
        assignee="Ralph",
        body="This will fail twice.",
    )
    git(git_repo, "add", ".jri/tasks/todo/failing-task.md")
    git(git_repo, "commit", "-m", "add failing task")

    # First run fails
    service1 = JriService(git_repo, opencode_client=FailedFakeOpenCodeClient())
    assert service1.start(iterations=1) == 0

    # Second run also fails
    service2 = JriService(git_repo, opencode_client=FailedFakeOpenCodeClient())
    assert service2.start(iterations=1) == 0

    # Task is still in todo, not escalated yet
    assert (git_repo / ".jri" / "tasks" / "todo" / "failing-task.md").exists()

    # Third run succeeds (task is still retryable)
    success_client = SuccessfulFakeOpenCodeClient()
    service3 = JriService(git_repo, opencode_client=success_client)
    assert service3.start(iterations=1) == 1
    assert (git_repo / ".jri" / "tasks" / "done" / "failing-task.md").exists()
    assert len(success_client.calls) == 1


def test_task_escalates_to_needs_human_after_three_failures(git_repo: Path) -> None:
    """After 3 failed attempts the task is escalated to needs human."""
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="failing-task",
        title="Failing task",
        priority=0,
        assignee="Ralph",
        body="This will fail three times.",
    )
    git(git_repo, "add", ".jri/tasks/todo/failing-task.md")
    git(git_repo, "commit", "-m", "add failing task")

    # First failure
    service1 = JriService(git_repo, opencode_client=FailedFakeOpenCodeClient())
    assert service1.start(iterations=1) == 0

    # Second failure
    service2 = JriService(git_repo, opencode_client=FailedFakeOpenCodeClient())
    assert service2.start(iterations=1) == 0

    # Third failure triggers auto-escalation
    fail_client3 = FailedFakeOpenCodeClient()
    service3 = JriService(git_repo, opencode_client=fail_client3)
    assert service3.start(iterations=1) == 0

    # Original task is back in todo, now blocked on a generated Human task
    assert (git_repo / ".jri" / "tasks" / "todo" / "failing-task.md").exists()
    todo_tasks = list_tasks(git_repo / ".jri" / "tasks" / "todo")
    human_tasks = [t for t in todo_tasks if t.metadata.assignee == "Human"]
    assert len(human_tasks) == 1
    original = parse_task_file(git_repo / ".jri" / "tasks" / "todo" / "failing-task.md")
    assert original.metadata.depends_on == [human_tasks[0].slug]
    assert "failing-task" in human_tasks[0].slug

    # Attempt history records three failures
    state = read_json(git_repo / ".jri" / "state.json")
    attempts = cast(list[dict[str, object]], state["attempts"])
    failed_for_task = [
        a
        for a in attempts
        if a.get("task_slug") == "failing-task" and a.get("outcome") == "failed"
    ]
    assert len(failed_for_task) == 3

    # Subsequent start does not retry the escalated task
    success_client = SuccessfulFakeOpenCodeClient()
    service4 = JriService(git_repo, opencode_client=success_client)
    assert service4.start(iterations=1) == 0
    assert success_client.calls == []


def test_failed_iteration_recovery_logs_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    client = FailedFakeOpenCodeClient()
    service = JriService(git_repo, opencode_client=client)

    monkeypatch.setattr(
        JriService,
        "_reset_runtime_state",
        lambda self_: (_ for _ in ()).throw(
            OSError("simulated reset failure during recovery")
        ),
    )

    completed = service.start(iterations=1)

    assert completed == 0
    assert (git_repo / ".jri" / "tasks" / "todo" / "failing-task.md").exists()

    failure_log_path = git_repo / ".jri" / "logs" / "recovery-failures.log"
    assert failure_log_path.exists()
    failure_log = failure_log_path.read_text(encoding="utf-8")
    assert "event=recovery-failure" in failure_log
    assert "task=failing-task" in failure_log
    assert "phase=recover-failed-iteration" in failure_log
    assert "error_type=OSError" in failure_log
    assert "simulated reset failure during recovery" in failure_log


def test_needs_human_recovery_logs_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="needs-human-task",
        title="Needs human task",
        priority=0,
        assignee="Ralph",
        body="This will need human help.",
    )
    git(git_repo, "add", ".jri/tasks/todo/needs-human-task.md")
    git(git_repo, "commit", "-m", "add needs human task")

    client = NeedsHumanFakeOpenCodeClient()
    service = JriService(git_repo, opencode_client=client)

    monkeypatch.setattr(
        JriService,
        "_reset_runtime_state",
        lambda self_: (_ for _ in ()).throw(
            OSError("simulated reset failure during recovery")
        ),
    )

    completed = service.start(iterations=1)

    assert completed == 0

    failure_log_path = git_repo / ".jri" / "logs" / "recovery-failures.log"
    assert failure_log_path.exists()
    failure_log = failure_log_path.read_text(encoding="utf-8")
    assert "event=recovery-failure" in failure_log
    assert "task=needs-human-task" in failure_log
    assert "phase=recover-needs-human-iteration" in failure_log
    assert "error_type=OSError" in failure_log
    assert "simulated reset failure during recovery" in failure_log


def test_stale_iteration_recovery_logs_failure_and_propagates_error(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="doing",
        slug="stale-task",
        title="Stale task",
        priority=0,
        assignee="Ralph",
        body="Was interrupted.",
    )
    git(git_repo, "add", ".jri/tasks/doing/stale-task.md")
    git(git_repo, "commit", "-m", "seed stale task")

    service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())

    import jri.core.service as service_module

    monkeypatch.setattr(
        service_module,
        "move_task",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("simulated move failure")),
    )

    with pytest.raises(OSError, match="simulated move failure"):
        service.start(iterations=1)

    failure_log_path = git_repo / ".jri" / "logs" / "recovery-failures.log"
    assert failure_log_path.exists()
    failure_log = failure_log_path.read_text(encoding="utf-8")
    assert "event=recovery-failure" in failure_log
    assert "task=stale-task" in failure_log
    assert "phase=recover-stale-iteration" in failure_log
    assert "error_type=OSError" in failure_log


def test_state_is_understandable_after_partial_recovery_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    client = FailedFakeOpenCodeClient()
    service = JriService(git_repo, opencode_client=client)

    monkeypatch.setattr(
        JriService,
        "_reset_runtime_state",
        lambda self_: (_ for _ in ()).throw(
            OSError("simulated reset failure during recovery")
        ),
    )

    completed = service.start(iterations=1)

    assert completed == 0

    # Task is back in todo (checkout and branch cleanup succeeded before reset)
    assert (git_repo / ".jri" / "tasks" / "todo" / "failing-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "doing" / "failing-task.md").exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "failing-task.md").exists()

    # Git is on default branch (checkout succeeded before reset)
    assert git(git_repo, "branch", "--show-current") == "main"

    # State is valid and loadable
    state = read_json(git_repo / ".jri" / "state.json")
    assert "iteration" in state
    assert "attempts" in state

    # Attempt records the failure
    attempts = cast(list[dict[str, object]], state["attempts"])
    assert len(attempts) == 1
    assert attempts[0]["task_slug"] == "failing-task"
    assert attempts[0]["outcome"] == "failed"

    # Recovery failure log explains what happened
    failure_log_path = git_repo / ".jri" / "logs" / "recovery-failures.log"
    assert failure_log_path.exists()
    failure_log = failure_log_path.read_text(encoding="utf-8")
    assert "event=recovery-failure" in failure_log
    assert "task=failing-task" in failure_log
    assert "phase=recover-failed-iteration" in failure_log


def test_successful_iteration_saves_diff_artifact(git_repo: Path) -> None:
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
    diff_path = git_repo / ".jri" / "logs" / "diffs" / "1-implement-file.diff"
    assert diff_path.exists()
    diff_text = diff_path.read_text(encoding="utf-8")
    assert "implemented.txt" in diff_text
    assert "+implemented" in diff_text


def test_diff_artifact_is_created_for_recovered_completion(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
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
        priority=1,
        assignee="Ralph",
        body="Complete task B.",
    )
    git(git_repo, "add", ".jri/tasks/todo")
    git(git_repo, "commit", "-m", "add retry tasks")

    first_client = SuccessfulFakeOpenCodeClient()
    first_service = JriService(git_repo, opencode_client=first_client)

    def interrupted_save_diff(iteration: int, task_slug: str) -> None:
        raise KeyboardInterrupt("simulated interruption during diff save")

    monkeypatch.setattr(first_service, "_save_diff_artifact", interrupted_save_diff)

    with pytest.raises(KeyboardInterrupt, match="simulated interruption"):
        first_service.start(iterations=1)

    assert (git_repo / ".jri" / "tasks" / "done" / "task-a.md").exists()
    diff_path = git_repo / ".jri" / "logs" / "diffs" / "1-task-a.diff"
    assert not diff_path.exists()

    retry_service = JriService(git_repo, opencode_client=SuccessfulFakeOpenCodeClient())
    assert retry_service.start(iterations=1) == 1

    assert diff_path.exists()
    diff_text = diff_path.read_text(encoding="utf-8")
    assert "implemented.txt" in diff_text
