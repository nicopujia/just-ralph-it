from importlib.resources import files
from pathlib import Path
from typing import Literal

import pytest

from jri.core.models import Task, TaskMetadata
from jri.core.tasks import (
    parse_task_file,
    select_next_task,
    validate_state_payload,
    validate_task_metadata,
)


def make_task(
    slug: str,
    *,
    priority: int = 1,
    assignee: Literal["Ralph", "Human"] = "Ralph",
    depends_on: list[str] | None = None,
) -> Task:
    return Task(
        path=Path(f"/tmp/{slug}.md"),
        slug=slug,
        metadata=TaskMetadata(
            title=slug.replace("-", " ").title(),
            priority=priority,
            assignee=assignee,
            depends_on=depends_on or [],
            acceptance_criteria=[],
        ),
        body="body",
    )


def test_select_next_task_uses_dependencies_priority_and_slug_order() -> None:
    tasks = [
        make_task("blocked", priority=0, depends_on=["missing"]),
        make_task("human-task", priority=0, assignee="Human"),
        make_task("beta", priority=1),
        make_task("alpha", priority=1),
    ]

    selected = select_next_task(tasks, done_slugs={"setup"}, doing_tasks=[])

    assert selected is not None
    assert selected.slug == "alpha"


def test_select_next_task_rejects_existing_doing_tasks() -> None:
    with pytest.raises(ValueError, match="already in progress"):
        select_next_task(
            [make_task("alpha")], done_slugs=set(), doing_tasks=[make_task("doing")]
        )


def test_validate_task_metadata_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="assignee"):
        validate_task_metadata({"title": "Bad task", "priority": 1})

    with pytest.raises(ValueError, match="assignee"):
        validate_task_metadata(
            {
                "title": "Bad task",
                "priority": 1,
                "assignee": "Robot",
                "depends_on": [],
                "acceptance_criteria": [],
            }
        )

    with pytest.raises(ValueError, match="priority"):
        validate_task_metadata(
            {
                "title": "Bad task",
                "priority": 9,
                "assignee": "Ralph",
                "depends_on": [],
                "acceptance_criteria": [],
            }
        )

    with pytest.raises(ValueError, match="depends_on"):
        validate_task_metadata(
            {
                "title": "Bad task",
                "priority": 1,
                "assignee": "Ralph",
                "depends_on": ["a", "a"],
                "acceptance_criteria": [],
            }
        )


def test_validate_state_payload_rejects_negative_iteration() -> None:
    with pytest.raises(ValueError, match="iteration"):
        validate_state_payload({"iteration": {"number": -1}})


def test_validate_state_payload_allows_runtime_process_metadata() -> None:
    validate_state_payload(
        {
            "iteration": {"number": 1},
            "process": {
                "loop_pid": 123,
                "child_pid": None,
                "log_path": ".jri/logs/ralph/1.log",
                "detached": True,
            },
        }
    )


def test_packaged_schemas_are_available() -> None:
    assert files("jri.schemas").joinpath("task-metadata.json").is_file()
    assert files("jri.schemas").joinpath("state.json").is_file()


def test_parse_task_file_reads_frontmatter_and_body(tmp_path: Path) -> None:
    task_path = tmp_path / "build-readme.md"
    task_path.write_text(
        "---\n"
        '{"title": "Build README", "priority": 1, '
        '"assignee": "Ralph", "depends_on": ["prep"], '
        '"acceptance_criteria": ["README exists"]}'
        "\n---\n\nWrite the README body.\n",
        encoding="utf-8",
    )

    task = parse_task_file(task_path)

    assert task.slug == "build-readme"
    assert task.metadata.depends_on == ["prep"]
    assert task.body == "Write the README body.\n"
