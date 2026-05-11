import os
import subprocess
import sys
from pathlib import Path

import pytest

from jri.checks.schema import main, validate_repo
from tests.conftest import run_cli
from tests.helpers import git, write_task


def _schema_env() -> dict[str, str]:
    return os.environ | {"PYTHONPATH": str(Path(__file__).parents[2] / "src")}


def _run_schema_module(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "jri.checks.schema", str(root)],
        capture_output=True,
        text=True,
        check=False,
        env=_schema_env(),
    )


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
    assert main([str(tmp_path)]) == 0


def test_validate_repo_accepts_current_lifecycle_task_directories(
    tmp_path: Path,
) -> None:
    for status in ("todo", "doing", "done"):
        task_path = tmp_path / ".jri" / "tasks" / status / f"{status}-quality.md"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text(
            "---\n"
            f'title: "{status.title()} quality"\n'
            "priority: 1\n"
            'assignee: "Ralph"\n'
            "depends_on: []\n"
            "acceptance_criteria:\n"
            f'  - "{status} task remains valid"\n'
            "---\n\n"
            f"Exercise the {status} lifecycle state.\n",
            encoding="utf-8",
        )

    validate_repo(tmp_path)


def test_schema_command_returns_nonzero_for_invalid_task_file(tmp_path: Path) -> None:
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

    result = _run_schema_module(tmp_path)

    assert result.returncode == 1
    assert "schema check failed" in result.stderr
    assert "assignee" in result.stderr
    assert result.stdout == ""


def test_validate_repo_rejects_lifecycle_task_without_acceptance_criteria(
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


def test_validate_repo_rejects_lifecycle_task_with_empty_acceptance_criteria(
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


def test_validate_repo_rejects_in_place_mutation_of_lifecycle_task(
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


def test_validate_repo_rejects_corrupted_state_file(tmp_path: Path) -> None:
    state_path = tmp_path / ".jri" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match="state.json is corrupted"):
        validate_repo(tmp_path)


def test_validate_repo_rejects_non_object_state_payload(tmp_path: Path) -> None:
    state_path = tmp_path / ".jri" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must contain an object"):
        validate_repo(tmp_path)


def test_validate_repo_rejects_invalid_state_content(tmp_path: Path) -> None:
    state_path = tmp_path / ".jri" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"started_at": "soon"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="state.json has invalid content"):
        validate_repo(tmp_path)


def test_schema_module_entry_point_returns_zero_for_valid_repo(tmp_path: Path) -> None:
    state_path = tmp_path / ".jri" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"attempts": []}\n', encoding="utf-8")

    result = _run_schema_module(tmp_path)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
