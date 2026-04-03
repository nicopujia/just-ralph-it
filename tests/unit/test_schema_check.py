from pathlib import Path

import pytest

from jri.checks.schema import main, validate_repo


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
    state_path.write_text('{"iteration": {"number": 1}}\n', encoding="utf-8")

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
