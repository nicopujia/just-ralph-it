import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ..models import Task
from ..service import JriService
from ..tasks import list_tasks

SLUG_RE = re.compile(r"^[a-zA-Z0-9][-a-zA-Z0-9_.]*$")


def _load_payload() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("tool payload must be a JSON object")
    return payload


def _print_result(message: str) -> None:
    sys.stdout.write(message)


def _assert_slug(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{name}` must be a non-empty string")
    slug = value.strip()
    if not SLUG_RE.match(slug):
        raise ValueError(
            f"`{name}` contains characters not allowed in task filenames; "
            "use only letters, digits, hyphens, dots, and underscores"
        )
    return slug


def _assert_string_list(name: str, value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"`{name}` must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"`{name}` must not contain duplicates")
    return value


def _assert_slug_list(name: str, value: Any) -> list[str] | None:
    items = _assert_string_list(name, value)
    if items is None:
        return None
    return [_assert_slug(name, item) for item in items]


def _ensure_expected_real_path(parent_dir: Path, child_name: str) -> Path:
    child_path = parent_dir / child_name
    child_path.mkdir(parents=True, exist_ok=True)
    real_child_path = child_path.resolve()
    if real_child_path != child_path:
        raise ValueError("refusing to write outside `.jri/tasks/`")
    return child_path


def _ensure_task_path_within(directory: Path, slug: str) -> Path:
    task_path = (directory / f"{slug}.md").resolve()
    try:
        task_path.relative_to(directory)
    except ValueError as exc:
        raise ValueError("refusing to write outside `.jri/tasks/`") from exc
    return task_path


def _read_task(task_path: Path) -> tuple[dict[str, Any], str]:
    source = task_path.read_text(encoding="utf-8")
    if not source.startswith("---\n"):
        raise ValueError(f"invalid task format: {task_path}")
    boundary = source.find("\n---\n", 4)
    if boundary == -1:
        raise ValueError(f"invalid task format: {task_path}")
    metadata_text = source[4:boundary]
    body = source[boundary + len("\n---\n") :]
    if body.startswith("\n"):
        body = body[1:]
    try:
        metadata = yaml.safe_load(metadata_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid task metadata YAML: {task_path}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"invalid task metadata object: {task_path}")
    depends_on = metadata.get("depends_on")
    if depends_on is not None:
        _assert_string_list("depends_on", depends_on)
    acceptance_criteria = metadata.get("acceptance_criteria")
    if acceptance_criteria is not None:
        _assert_string_list("acceptance_criteria", acceptance_criteria)
    return metadata, body


def _serialize_task(metadata: dict[str, Any], body: str) -> str:
    frontmatter = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=False).strip()
    return f"---\n{frontmatter}\n---\n\n{body}"


def _draft_task_dirs(root: Path) -> tuple[Path, Path, Path, Path]:
    repo_root = root.resolve()
    jri_dir = _ensure_expected_real_path(repo_root, ".jri")
    tasks_dir = _ensure_expected_real_path(jri_dir, "tasks")
    draft_dir = _ensure_expected_real_path(tasks_dir, "draft")
    todo_dir = _ensure_expected_real_path(tasks_dir, "todo")
    doing_dir = _ensure_expected_real_path(tasks_dir, "doing")
    done_dir = _ensure_expected_real_path(tasks_dir, "done")
    return draft_dir, todo_dir, doing_dir, done_dir


def _slugify(title: str) -> str:
    slug = title.strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", slug)
    if not slug:
        raise ValueError("could not derive a valid slug from title; pass `slug`")
    return slug


def _run_upsert_task(payload: dict[str, Any]) -> str:
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


def _run_rename_task(payload: dict[str, Any]) -> str:
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


def _run_delete_task(payload: dict[str, Any]) -> str:
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


def _run_promote_tasks(payload: dict[str, Any]) -> str:
    slugs = _assert_slug_list("slugs", payload.get("slugs")) or []
    check_only = payload.get("check_only", False)
    if not isinstance(check_only, bool):
        raise ValueError("`check_only` must be a boolean")

    service = JriService(Path.cwd())
    if check_only:
        selected = service.check_draft_promotion(slugs=slugs)
        lines = [f"Promotion check passed for {len(selected)} draft task(s)."]
    else:
        selected = service.promote_drafts(slugs=slugs)
        lines = [f"Promoted {len(selected)} draft task(s) to todo."]
    lines.extend(f"  - {task.slug}" for task in selected)
    return "\n".join(lines)


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


def _run_read_tasks(payload: dict[str, Any]) -> str:
    slugs = _assert_slug_list("slugs", payload.get("slugs"))
    if not slugs:
        raise ValueError("`slugs` must be a non-empty list of task slugs")

    service = JriService(Path.cwd())
    tasks_by_status = service.status()
    tasks_by_slug = {
        task.slug: task for tasks in tasks_by_status.values() for task in tasks
    }
    missing = [slug for slug in slugs if slug not in tasks_by_slug]
    if missing:
        raise ValueError(f"task not found: {', '.join(missing)}")

    result = [_task_to_payload(tasks_by_slug[slug]) for slug in slugs]
    return json.dumps(result, indent=2) + "\n"


def _run_list_tasks(payload: dict[str, Any]) -> str:
    status = payload.get("status")
    service = JriService(Path.cwd())
    if status is None:
        tasks = [task for tasks in service.status().values() for task in tasks]
    else:
        if status not in {"draft", "todo", "doing", "done"}:
            raise ValueError("`status` must be one of draft, todo, doing, done")
        tasks_dir = service.paths.tasks_dir / status
        tasks = list_tasks(tasks_dir, git_repo=service.git)
    return json.dumps([_task_to_payload(task) for task in tasks], indent=2) + "\n"


def _run_ralph_result(payload: dict[str, Any]) -> str:
    result = payload.get("result")
    if result not in {"completed", "incomplete", "needs_human"}:
        raise ValueError("invalid result")
    if result == "needs_human" and (
        not payload.get("blocker") or payload.get("human_task") is None
    ):
        raise ValueError("needs_human requires blocker and human_task")

    output_path = os.environ.get("JRI_RESULT_PATH")
    if not output_path:
        return "JRI_RESULT_PATH not set"

    result_payload = {"result": result}
    for key in ("summary", "learnings", "blocker", "human_task"):
        value = payload.get(key)
        if value:
            result_payload[key] = value

    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(result_payload, handle, indent=2)
        handle.write("\n")
    return f"Result recorded: {result}"


_HANDLERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "list-tasks": _run_list_tasks,
    "read-tasks": _run_read_tasks,
    "upsert-task": _run_upsert_task,
    "rename-task": _run_rename_task,
    "delete-task": _run_delete_task,
    "promote-tasks": _run_promote_tasks,
    "ralph-result": _run_ralph_result,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in _HANDLERS:
        available = ", ".join(sorted(_HANDLERS))
        print(f"expected one tool name ({available})", file=sys.stderr)
        return 2

    try:
        payload = _load_payload()
        _print_result(_HANDLERS[argv[0]](payload))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
