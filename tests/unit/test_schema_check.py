from pathlib import Path

import pytest

from jri.checks.schema import main, validate_repo
from tests.conftest import run_cli
from tests.helpers import git, write_task


def test_validate_repo_accepts_valid_jri_tree(tmp_path: Path) -> None:
    task_path = tmp_path / ".jri" / "tasks" / "todo" / "quality-gate.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text(
        "---\n"
        'title: "Add quality gate"\n'
        "priority: 0\n"
        'assignee: "Ralph"\n'
        "depends_on: []\n"
        "acceptance_criteria:\n"
        '  - "make check passes"\n'
        "---\n\n"
        "Implement the quality gate.\n",
        encoding="utf-8",
    )
    state_path = tmp_path / ".jri" / "state.json"
    state_path.write_text('{"attempts": []}\n', encoding="utf-8")

    validate_repo(tmp_path)


def test_validate_repo_allows_draft_task_without_acceptance_criteria(
    tmp_path: Path,
) -> None:
    task_path = tmp_path / ".jri" / "tasks" / "draft" / "clarify-scope.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text(
        "---\n"
        'title: "Clarify scope"\n'
        "priority: 1\n"
        'assignee: "Ralph"\n'
        "depends_on: []\n"
        "---\n\n"
        "Draft the scope.\n",
        encoding="utf-8",
    )

    validate_repo(tmp_path)


def test_main_returns_nonzero_for_invalid_task_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_path = tmp_path / ".jri" / "tasks" / "todo" / "quality-gate.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text(
        "---\n"
        'title: "Add quality gate"\n'
        "priority: 0\n"
        'assignee: "Robot"\n'
        "depends_on: []\n"
        "acceptance_criteria: []\n"
        "---\n\n"
        "Implement the quality gate.\n",
        encoding="utf-8",
    )

    assert main([str(tmp_path)]) == 1
    assert "schema check failed" in capsys.readouterr().err


def test_validate_repo_rejects_promoted_task_without_acceptance_criteria(
    tmp_path: Path,
) -> None:
    task_path = tmp_path / ".jri" / "tasks" / "todo" / "quality-gate.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text(
        "---\n"
        'title: "Add quality gate"\n'
        "priority: 0\n"
        'assignee: "Ralph"\n'
        "depends_on: []\n"
        "---\n\n"
        "Implement the quality gate.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="acceptance_criteria"):
        validate_repo(tmp_path)


def test_validate_repo_rejects_promoted_task_with_empty_acceptance_criteria(
    tmp_path: Path,
) -> None:
    task_path = tmp_path / ".jri" / "tasks" / "todo" / "quality-gate.md"
    task_path.parent.mkdir(parents=True)
    task_path.write_text(
        "---\n"
        'title: "Add quality gate"\n'
        "priority: 0\n"
        'assignee: "Ralph"\n'
        "depends_on: []\n"
        "acceptance_criteria: []\n"
        "---\n\n"
        "Implement the quality gate.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="acceptance_criteria"):
        validate_repo(tmp_path)


def test_validate_repo_rejects_in_place_mutation_of_promoted_task(
    git_repo: Path,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="quality-gate",
        title="Add quality gate",
        priority=0,
        assignee="Ralph",
        body="Implement the quality gate.\n",
        acceptance_criteria=["make check passes"],
    )
    git(git_repo, "add", ".jri/tasks/todo/quality-gate.md")
    git(git_repo, "commit", "-m", "add quality gate task")

    task_path = git_repo / ".jri" / "tasks" / "todo" / "quality-gate.md"
    task_path.write_text(
        task_path.read_text(encoding="utf-8") + "\nMutated in place.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="modified in place"):
        validate_repo(git_repo)
