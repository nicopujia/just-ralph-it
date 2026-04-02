import json
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import yaml
from jsonschema import Draft202012Validator

from .models import Task, TaskMetadata


@cache
def _load_schema(name: str) -> dict[str, object]:
    schema_path = files("jri.core.schemas").joinpath(name)
    payload = json.loads(schema_path.read_text(encoding="utf-8"))
    return cast(dict[str, object], payload)


@cache
def _validator(name: str) -> Any:
    return Draft202012Validator(_load_schema(name))


def validate_task_metadata(payload: dict[str, object]) -> TaskMetadata:
    errors = sorted(
        _validator("task-metadata.json").iter_errors(payload),
        key=lambda error: error.path,
    )
    if errors:
        joined = ", ".join(_format_error(error.path, error.message) for error in errors)
        raise ValueError(joined)

    depends_on = payload.get("depends_on")
    acceptance_criteria = payload.get("acceptance_criteria")
    assignee = payload["assignee"]
    priority = payload["priority"]
    depends_on_list = cast(
        list[str], depends_on if isinstance(depends_on, list) else []
    )
    criteria_list = cast(
        list[str], acceptance_criteria if isinstance(acceptance_criteria, list) else []
    )
    return TaskMetadata(
        title=str(payload["title"]),
        priority=priority if isinstance(priority, int) else 0,
        assignee=cast("Any", assignee),
        depends_on=depends_on_list,
        acceptance_criteria=criteria_list,
    )


def validate_state_payload(payload: dict[str, object]) -> None:
    errors = sorted(
        _validator("state.json").iter_errors(payload),
        key=lambda error: error.path,
    )
    if errors:
        joined = ", ".join(_format_error(error.path, error.message) for error in errors)
        raise ValueError(joined)


def parse_task_file(path: Path) -> Task:
    text = path.read_text(encoding="utf-8")
    metadata_payload, body = _split_frontmatter(text)
    metadata = validate_task_metadata(metadata_payload)
    return Task(path=path, slug=path.stem, metadata=metadata, body=body)


def list_tasks(directory: Path) -> list[Task]:
    if not directory.exists():
        return []
    return sorted(
        (parse_task_file(path) for path in directory.glob("*.md")),
        key=lambda task: task.slug,
    )


def select_next_task(
    tasks: list[Task], *, done_slugs: set[str], doing_tasks: list[Task]
) -> Task | None:
    if doing_tasks:
        raise ValueError("a task is already in progress")

    eligible = [
        task
        for task in tasks
        if task.metadata.assignee == "Ralph"
        and set(task.metadata.depends_on).issubset(done_slugs)
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda task: (task.metadata.priority, task.slug))
    return eligible[0]


def move_task(task: Task, destination_dir: Path) -> Task:
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / task.path.name
    task.path.replace(destination)
    return Task(
        path=destination, slug=task.slug, metadata=task.metadata, body=task.body
    )


def dump_task(task: Task) -> str:
    payload = {
        "title": task.metadata.title,
        "priority": task.metadata.priority,
        "assignee": task.metadata.assignee,
        "depends_on": task.metadata.depends_on,
        "acceptance_criteria": task.metadata.acceptance_criteria,
    }
    return "---\n" + json.dumps(payload, indent=2) + "\n---\n\n" + task.body


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        raise ValueError("task file must start with YAML frontmatter")

    boundary = text.find("\n---\n", 4)
    if boundary == -1:
        raise ValueError("task file must end frontmatter with ---")

    metadata_text = text[4:boundary]
    body = text[boundary + len("\n---\n") :]
    if body.startswith("\n"):
        body = body[1:]

    loaded = yaml.safe_load(metadata_text)
    if not isinstance(loaded, dict):
        raise ValueError("task frontmatter must be an object")
    return cast(dict[str, object], loaded), body


def _format_error(path: Any, message: str) -> str:
    parts = [str(part) for part in path]
    if parts:
        return f"{'.'.join(parts)}: {message}"
    return message
