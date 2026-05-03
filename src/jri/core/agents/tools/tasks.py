import json
from pathlib import Path
from typing import Any

from ...models import Task
from ...tasks import list_tasks
from ._validation import (
    _apply_exact_edits,
    _assert_exact_edits,
    _assert_slug,
    _assert_slug_list,
    _assert_string_list,
    _diff_text,
    _draft_task_dirs,
    _ensure_task_path_within,
    _read_task,
    _read_task_source,
    _serialize_task,
    _service,
    _slugify,
)


def run_upsert_task(payload: dict[str, Any]) -> str:
    title = payload.get("title")
    body = payload.get("body")
    assignee = payload.get("assignee")
    priority = payload.get("priority")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("`title` must be a non-empty string")
    if len(title) > 75:
        raise ValueError("`title` must be 75 characters or fewer")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("`body` must be a non-empty string")
    if assignee not in {"Ralph", "Human"}:
        raise ValueError("`assignee` must be either `Ralph` or `Human`")
    if (
        not isinstance(priority, int)
        or isinstance(priority, bool)
        or not 0 <= priority <= 4
    ):
        raise ValueError("`priority` must be an integer from 0 to 4")

    slug_value = payload.get("slug")
    task_slug = (
        _assert_slug("slug", slug_value) if slug_value is not None else _slugify(title)
    )
    depends_on = _assert_string_list("depends_on", payload.get("depends_on")) or []
    acceptance_criteria = _assert_string_list(
        "acceptance_criteria", payload.get("acceptance_criteria")
    )
    if not acceptance_criteria:
        raise ValueError(
            "`acceptance_criteria` must be a non-empty list of non-empty strings"
        )

    draft_dir, _, _, _ = _draft_task_dirs(Path.cwd())
    task_path = _ensure_task_path_within(draft_dir, task_slug)
    if task_path.exists() and task_path.is_symlink():
        raise ValueError("refusing to overwrite symlinked draft task")

    metadata = {
        "title": title.strip(),
        "priority": priority,
        "assignee": assignee,
        "depends_on": depends_on,
    }
    if acceptance_criteria is not None:
        metadata["acceptance_criteria"] = acceptance_criteria

    action = "updated" if task_path.exists() else "created"
    task_path.write_text(_serialize_task(metadata, body), encoding="utf-8")
    return f"{action} draft task: .jri/tasks/draft/{task_slug}.md"


def run_rename_task(payload: dict[str, Any]) -> str:
    from_slug = _assert_slug("from_slug", payload.get("from_slug"))
    to_slug = _assert_slug("to_slug", payload.get("to_slug"))
    if from_slug == to_slug:
        return f"draft task already uses slug: .jri/tasks/draft/{from_slug}.md"

    draft_dir, todo_dir, doing_dir, done_dir = _draft_task_dirs(Path.cwd())
    from_path = _ensure_task_path_within(draft_dir, from_slug)
    if not from_path.exists():
        raise ValueError(f"draft task does not exist: .jri/tasks/draft/{from_slug}.md")
    if from_path.is_symlink():
        raise ValueError("refusing to rename symlinked draft task")

    for directory, label in (
        (draft_dir, "draft"),
        (todo_dir, "todo"),
        (doing_dir, "doing"),
        (done_dir, "done"),
    ):
        collision_path = _ensure_task_path_within(directory, to_slug)
        if collision_path.exists():
            raise ValueError(
                f"target slug already exists in .jri/tasks/{label}/{to_slug}.md"
            )

    updated_dependencies: list[str] = []
    for task_path in sorted(draft_dir.glob("*.md")):
        if task_path.is_symlink():
            raise ValueError("refusing to inspect symlinked draft task entry")
        if task_path == from_path:
            continue
        metadata, body = _read_task(task_path)
        depends_on = metadata.get("depends_on", [])
        if from_slug not in depends_on:
            continue
        metadata["depends_on"] = [
            to_slug if slug == from_slug else slug for slug in depends_on
        ]
        task_path.write_text(_serialize_task(metadata, body), encoding="utf-8")
        updated_dependencies.append(task_path.name)

    to_path = _ensure_task_path_within(draft_dir, to_slug)
    from_path.rename(to_path)
    message = (
        f"renamed draft task: .jri/tasks/draft/{from_slug}.md -> "
        f".jri/tasks/draft/{to_slug}.md"
    )
    if updated_dependencies:
        message += (
            f"; updated depends_on in {len(updated_dependencies)} draft task(s): "
            + ", ".join(updated_dependencies)
        )
    return message


def run_delete_task(payload: dict[str, Any]) -> str:
    slug = _assert_slug("slug", payload.get("slug"))
    draft_dir, todo_dir, doing_dir, done_dir = _draft_task_dirs(Path.cwd())
    task_path = _ensure_task_path_within(draft_dir, slug)
    if not task_path.exists():
        for directory, label in (
            (todo_dir, "todo"),
            (doing_dir, "doing"),
            (done_dir, "done"),
        ):
            promoted_path = _ensure_task_path_within(directory, slug)
            if promoted_path.exists():
                raise ValueError(
                    f"refusing to delete promoted task: .jri/tasks/{label}/{slug}.md"
                )
        raise ValueError(f"draft task does not exist: .jri/tasks/draft/{slug}.md")
    if task_path.is_symlink():
        raise ValueError("refusing to delete symlinked draft task")

    blockers: list[str] = []
    for other_path in sorted(draft_dir.glob("*.md")):
        if other_path.is_symlink():
            raise ValueError("refusing to inspect symlinked draft task entry")
        if other_path == task_path:
            continue
        metadata, _body = _read_task(other_path)
        depends_on = metadata.get("depends_on", [])
        if slug in depends_on:
            blockers.append(other_path.name)
    if blockers:
        raise ValueError(
            f"refusing to delete draft task with dependents: {', '.join(blockers)}"
        )

    task_path.unlink()
    return f"deleted draft task: .jri/tasks/draft/{slug}.md"


def run_edit_draft_task(payload: dict[str, Any]) -> str:
    slug = _assert_slug("slug", payload.get("slug"))
    edits = _assert_exact_edits(payload)
    draft_dir, todo_dir, doing_dir, done_dir = _draft_task_dirs(Path.cwd())
    task_path = _ensure_task_path_within(draft_dir, slug)
    if not task_path.exists():
        for directory, label in (
            (todo_dir, "todo"),
            (doing_dir, "doing"),
            (done_dir, "done"),
        ):
            promoted_path = _ensure_task_path_within(directory, slug)
            if promoted_path.exists():
                raise ValueError(
                    f"refusing to edit promoted task: .jri/tasks/{label}/{slug}.md"
                )
        raise ValueError(f"draft task does not exist: .jri/tasks/draft/{slug}.md")
    if task_path.is_symlink():
        raise ValueError("refusing to edit symlinked draft task")

    source = task_path.read_text(encoding="utf-8")
    updated, replacements = _apply_exact_edits(source, edits)
    _read_task_source(task_path, updated)
    task_path.write_text(updated, encoding="utf-8")
    relative = f".jri/tasks/draft/{slug}.md"
    result = {
        "path": relative,
        "replacements": replacements,
        "diff": _diff_text(relative, source, updated),
    }
    return json.dumps(result, indent=2) + "\n"


def _task_to_payload(task: Task) -> dict[str, object]:
    return {
        "status": task.path.parent.name,
        "slug": task.slug,
        "path": str(task.path),
        "title": task.metadata.title,
        "priority": task.metadata.priority,
        "assignee": task.metadata.assignee,
        "depends_on": task.metadata.depends_on,
        "acceptance_criteria": task.metadata.acceptance_criteria,
        "body": task.body,
    }


def run_read_tasks(payload: dict[str, Any]) -> str:
    slugs = _assert_slug_list("slugs", payload.get("slugs"))
    if not slugs:
        raise ValueError("`slugs` must be a non-empty list of task slugs")

    service = _service(Path.cwd())
    tasks_by_status = service.status()
    tasks_by_slug = {
        task.slug: task for tasks in tasks_by_status.values() for task in tasks
    }
    missing = [slug for slug in slugs if slug not in tasks_by_slug]
    if missing:
        raise ValueError(f"task not found: {', '.join(missing)}")

    result = [_task_to_payload(tasks_by_slug[slug]) for slug in slugs]
    return json.dumps(result, indent=2) + "\n"


def run_list_tasks(payload: dict[str, Any]) -> str:
    status = payload.get("status")
    service = _service(Path.cwd())
    if status is None:
        tasks = [task for tasks in service.status().values() for task in tasks]
    else:
        if status not in {"draft", "todo", "doing", "done"}:
            raise ValueError("`status` must be one of draft, todo, doing, done")
        tasks_dir = service.paths.tasks_dir / status
        tasks = list_tasks(tasks_dir, git_repo=service.git)
    return json.dumps([_task_to_payload(task) for task in tasks], indent=2) + "\n"
