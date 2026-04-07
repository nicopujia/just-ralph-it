from pathlib import Path

from tests.conftest import run_cli
from tests.helpers import git, write_task


def _init(repo: Path) -> None:
    run_cli(["init"], cwd=repo)


def test_status_shows_counts_and_human_tasks(git_repo: Path, capsys) -> None:
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


def test_status_no_human_tasks(git_repo: Path, capsys) -> None:
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


def test_status_empty_project(git_repo: Path, capsys) -> None:
    _init(git_repo)

    rc = run_cli(["status"], cwd=git_repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Tasks: 0 total" in out
    assert "draft" in out
    assert "todo" in out
    assert "doing" in out
    assert "done" in out
    assert "No tasks assigned to Human." in out


def test_status_shows_human_tasks_across_all_states(git_repo: Path, capsys) -> None:
    """Human tasks in any state (draft, todo, doing, done) are shown."""
    _init(git_repo)
    write_task(
        git_repo,
        status="draft",
        slug="human-draft",
        title="Human draft",
        priority=0,
        assignee="Human",
        body="draft",
    )
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
    # All Human tasks should appear with their status
    assert "[draft ] [P0] human-draft" in out
    assert "[todo  ] [P1] human-todo" in out
    assert "[doing ] [P2] human-doing" in out
    assert "[done  ] [P3] human-done" in out


def test_status_rejects_in_place_mutation_of_promoted_task(
    git_repo: Path, capsys
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


def test_status_shows_metrics_summary(git_repo: Path, capsys) -> None:
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


def test_status_hides_metrics_when_none(git_repo: Path, capsys) -> None:
    """No metrics line is shown when metrics.json does not exist."""
    _init(git_repo)

    rc = run_cli(["status"], cwd=git_repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert "metrics:" not in out
