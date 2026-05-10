import subprocess
from pathlib import Path

import pytest

from jri.core.models import AttemptState
from jri.core.service import JriService
from tests.conftest import run_cli
from tests.helpers import git, write_task


def _init(repo: Path) -> None:
    run_cli(["init"], cwd=repo)


def _write_graph_node(
    repo: Path,
    semantic_path: str,
    *,
    title: str = "Node",
    state: str = "active",
    archive_reason: str | None = None,
) -> Path:
    node_path = repo / ".jri" / "graph" / semantic_path / "NODE.md"
    node_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"title: {title}", f"state: {state}"]
    if archive_reason is not None:
        lines.append(f"archive_reason: {archive_reason}")
    lines.extend(["---", "", "Body\n"])
    node_path.write_text("\n".join(lines), encoding="utf-8")
    return node_path


def test_status_shows_counts_and_human_tasks(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(git_repo)
    write_task(
        git_repo,
        status="todo",
        slug="human-task",
        title="Human task",
        priority=1,
        assignee="Human",
        body="do it",
    )
    write_task(
        git_repo,
        status="todo",
        slug="ralph-task",
        title="Ralph task",
        priority=0,
        assignee="Ralph",
        body="do it",
    )
    write_task(
        git_repo,
        status="done",
        slug="finished",
        title="Finished",
        priority=2,
        assignee="Ralph",
        body="done",
    )
    write_task(
        git_repo,
        status="todo",
        slug="another-human",
        title="Another human",
        priority=0,
        assignee="Human",
        body="do it",
    )

    rc = run_cli(["status"], cwd=git_repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Tasks: 4 total" in out
    assert "todo" in out
    assert "3" in out  # 3 todo tasks
    assert "done" in out
    assert "1" in out  # 1 done task
    # Human tasks sorted by priority then status then slug
    assert "[todo  ] [P0] another-human" in out
    assert "[todo  ] [P1] human-task" in out
    # Ralph tasks NOT in human section
    assert "ralph-task" not in out.split("Tasks assigned to Human")[1]


def test_status_explains_actionable_human_blocker(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(git_repo)
    write_task(
        git_repo,
        status="todo",
        slug="needs-human-task",
        title="Needs human task",
        priority=0,
        assignee="Ralph",
        body="Blocked until a Human task is complete.",
        depends_on=["needs-human-task--needs-human"],
    )
    write_task(
        git_repo,
        status="todo",
        slug="needs-human-task--needs-human",
        title="Provide missing input",
        priority=0,
        assignee="Human",
        body="Provide the missing input.",
    )

    rc = run_cli(["status"], cwd=git_repo)

    assert rc == 0
    out = capsys.readouterr().out
    assert (
        "Action needed: complete Human task needs-human-task--needs-human, "
        "then run `jri complete-human needs-human-task--needs-human`." in out
    )


def test_status_explains_completed_human_blocker_leaves_ralph_retry(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(git_repo)
    write_task(
        git_repo,
        status="todo",
        slug="needs-human-task",
        title="Needs human task",
        priority=0,
        assignee="Ralph",
        body="Retry after the Human task is complete.",
        depends_on=["needs-human-task--needs-human"],
    )
    write_task(
        git_repo,
        status="done",
        slug="needs-human-task--needs-human",
        title="Provide missing input",
        priority=0,
        assignee="Human",
        body="Provided the missing input.",
    )

    rc = run_cli(["status"], cwd=git_repo)

    assert rc == 0
    out = capsys.readouterr().out
    assert "[done  ] [P0] needs-human-task--needs-human" in out
    assert "Action needed: run `jri start` to retry needs-human-task." in out


def test_status_no_human_tasks(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(git_repo)
    write_task(
        git_repo,
        status="todo",
        slug="ralph-only",
        title="Ralph only",
        priority=0,
        assignee="Ralph",
        body="do it",
    )

    rc = run_cli(["status"], cwd=git_repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert "No tasks assigned to Human." in out


def test_status_empty_project(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(git_repo)

    rc = run_cli(["status"], cwd=git_repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Tasks: 0 total" in out
    assert "Ralph: not running" in out
    assert "todo" in out
    assert "doing" in out
    assert "done" in out
    assert "No tasks assigned to Human." in out


def test_status_shows_ralph_running_state(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(git_repo)
    sleeper = subprocess.Popen(["sleep", "30"])
    service = JriService(git_repo)
    service.state_store.save_process(
        loop_pid=sleeper.pid,
        child_pid=None,
        log_path=None,
        detached=True,
    )
    service.state_store.start_attempt(
        AttemptState(number=1, task_slug="ralph-task", branch="ralph", started_at=1)
    )
    service.paths.stop_signal_path.parent.mkdir(parents=True, exist_ok=True)
    service.paths.stop_signal_path.write_text("requested\n", encoding="utf-8")

    try:
        rc = run_cli(["status"], cwd=git_repo)
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)

    assert rc == 0
    out = capsys.readouterr().out
    assert "Ralph: running (detached) on ralph-task, stop requested" in out


def test_status_shows_stale_tracked_ralph_process(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(git_repo)
    process = subprocess.Popen(["sleep", "0"])
    process.wait(timeout=5)
    service = JriService(git_repo)
    service.state_store.save_process(
        loop_pid=process.pid,
        child_pid=None,
        log_path=None,
        detached=False,
    )

    rc = run_cli(["status"], cwd=git_repo)

    assert rc == 0
    out = capsys.readouterr().out
    assert "Ralph: not running (previous run was interrupted)" in out
    assert (
        "Action needed: run `jri start --force` "
        "to recover interrupted Ralph state." in out
    )


def test_status_shows_stale_doing_without_tracked_process(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(git_repo)
    write_task(
        git_repo,
        status="doing",
        slug="stale-task",
        title="Stale task",
        priority=0,
        assignee="Ralph",
        body="A crashed run left this in doing.",
    )

    rc = run_cli(["status"], cwd=git_repo)

    assert rc == 0
    out = capsys.readouterr().out
    assert "Ralph: not running (task left in doing)" in out
    assert (
        "Action needed: run `jri start --force` "
        "to recover interrupted Ralph state." in out
    )


def test_status_shows_human_tasks_across_tracked_states(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Human tasks in tracked states (todo, doing, done) are shown."""
    _init(git_repo)
    write_task(
        git_repo,
        status="todo",
        slug="human-todo",
        title="Human todo",
        priority=1,
        assignee="Human",
        body="todo",
    )
    write_task(
        git_repo,
        status="doing",
        slug="human-doing",
        title="Human doing",
        priority=2,
        assignee="Human",
        body="doing",
    )
    write_task(
        git_repo,
        status="done",
        slug="human-done",
        title="Human done",
        priority=3,
        assignee="Human",
        body="done",
    )

    rc = run_cli(["status"], cwd=git_repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Actionable Human tasks:" in out
    assert "Completed Human tasks:" in out
    assert "[todo  ] [P1] human-todo" in out
    assert "[doing ] [P2] human-doing" in out
    assert "[done  ] [P3] human-done" in out


def test_status_rejects_in_place_mutation_of_promoted_task(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(git_repo)
    task_path = write_task(
        git_repo,
        status="todo",
        slug="ralph-only",
        title="Ralph only",
        priority=0,
        assignee="Ralph",
        body="do it",
    )
    git(git_repo, "add", ".jri/tasks/todo/ralph-only.md")
    git(git_repo, "commit", "-m", "add ralph task")
    task_path.write_text(
        task_path.read_text(encoding="utf-8") + "\nmutated\n",
        encoding="utf-8",
    )

    rc = run_cli(["status"], cwd=git_repo)

    assert rc == 1
    assert "modified in place" in capsys.readouterr().err


def test_status_shows_metrics_summary(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Metrics summary is displayed when metrics exist."""
    import json

    _init(git_repo)
    # Write a metrics file with some entries
    metrics_path = git_repo / ".jri" / "metrics.json"
    metrics_path.write_text(
        json.dumps(
            [
                {"task": "a", "ts": "t1", "result": "pass"},
                {"task": "b", "ts": "t2", "result": "pass"},
                {"task": "c", "ts": "t3", "result": "fail"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rc = run_cli(["status"], cwd=git_repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert "metrics: 3 runs, 2 pass, 1 fail (67% pass rate)" in out


def test_status_hides_metrics_when_none(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No metrics line is shown when metrics.json does not exist."""
    _init(git_repo)

    rc = run_cli(["status"], cwd=git_repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert "metrics:" not in out


def test_status_shows_graph_counts(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(git_repo)
    _write_graph_node(git_repo, "product", title="Product")
    _write_graph_node(
        git_repo,
        "product/old",
        title="Old",
        state="archived",
        archive_reason="Replaced",
    )

    rc = run_cli(["status"], cwd=git_repo)

    assert rc == 0
    out = capsys.readouterr().out
    assert "Graph: 1 active, 1 archived" in out
    assert "malformed" not in out


def test_status_reports_invalid_graph_errors(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init(git_repo)
    (git_repo / ".jri" / "graph" / "product").mkdir(parents=True)

    rc = run_cli(["status"], cwd=git_repo)

    assert rc == 0
    out = capsys.readouterr().out
    assert "Graph: 0 active, 0 archived, 1 malformed" in out
    assert "product: missing NODE.md" in out
