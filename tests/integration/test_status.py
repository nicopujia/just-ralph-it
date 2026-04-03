from pathlib import Path

from tests.conftest import run_cli
from tests.helpers import write_task


def _init(repo: Path) -> None:
    run_cli(["init"], cwd=repo)


def test_status_shows_counts_and_human_todos(git_repo: Path, capsys) -> None:
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
    # Human todos sorted by priority then slug
    assert "[P0] another-human" in out
    assert "[P1] human-task" in out
    # Ralph tasks NOT in human section
    assert "ralph-task" not in out.split("Todo tasks assigned to Human")[1]


def test_status_no_human_todos(git_repo: Path, capsys) -> None:
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
    assert "No todo tasks assigned to Human." in out


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
    assert "No todo tasks assigned to Human." in out
