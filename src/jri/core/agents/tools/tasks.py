import json
from pathlib import Path

from ...models import Task
from ...tasks import list_tasks
from ._validation import (
    assert_slug,
    assert_slug_list,
    assert_string_list,
    ensure_task_path_within,
    serialize_task,
    service,
    slugify,
    task_dirs,
)


def run_upsert_task(payload: dict[str, object]) -> str:
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
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 4:
        raise ValueError("`priority` must be an integer from 0 to 4")

    slug_value = payload.get("slug")
    task_slug = assert_slug("slug", slug_value) if slug_value is not None else slugify(title)
    depends_on = assert_string_list("depends_on", payload.get("depends_on")) or []
    acceptance_criteria = assert_string_list("acceptance_criteria", payload.get("acceptance_criteria"))
    if not acceptance_criteria:
        raise ValueError("`acceptance_criteria` must be a non-empty list of non-empty strings")

    todo_dir, _, _ = task_dirs(Path.cwd())
    task_path = ensure_task_path_within(todo_dir, task_slug)
    if task_path.exists() and task_path.is_symlink():
        raise ValueError("refusing to overwrite symlinked todo task")
    if task_path.exists():
        raise ValueError("refusing to overwrite existing todo task")

    metadata = {"title": title.strip(), "priority": priority, "assignee": assignee, "depends_on": depends_on}
    metadata["acceptance_criteria"] = acceptance_criteria

    task_path.write_text(serialize_task(metadata, body), encoding="utf-8")
    return f"created todo task: .jri/tasks/todo/{task_slug}.md"


def run_read_tasks(payload: dict[str, object]) -> str:
    slugs = assert_slug_list("slugs", payload.get("slugs"))
    if not slugs:
        raise ValueError("`slugs` must be a non-empty list of task slugs")

    jri_service = service(Path.cwd())
    tasks_by_status = jri_service.status()
    tasks_by_slug = {task.slug: task for tasks in tasks_by_status.values() for task in tasks}
    missing = [slug for slug in slugs if slug not in tasks_by_slug]
    if missing:
        raise ValueError(f"task not found: {', '.join(missing)}")

    result = [_task_to_payload(tasks_by_slug[slug]) for slug in slugs]
    return json.dumps(result, indent=2) + "\n"


def run_list_tasks(payload: dict[str, object]) -> str:
    status = payload.get("status")
    jri_service = service(Path.cwd())
    if status is None:
        tasks = [task for tasks in jri_service.status().values() for task in tasks]
    else:
        if not isinstance(status, str) or status not in {"todo", "doing", "done"}:
            raise ValueError("`status` must be one of todo, doing, done")
        tasks_dir = jri_service.paths.tasks_dir / status
        tasks = list_tasks(tasks_dir, git_repo=jri_service.git)
    return json.dumps([_task_to_payload(task) for task in tasks], indent=2) + "\n"


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
