import json
import re
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import yaml
from jsonschema import Draft202012Validator
from yaml.events import DocumentEndEvent

from .git import GitRepo
from .models import Task, TaskMetadata

_PROMOTED_TASK_STATUSES = frozenset({"todo", "doing", "done"})


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


_VALID_SLUG = re.compile(r"^[a-zA-Z0-9][-a-zA-Z0-9_.]*$")


def parse_task_file(path: Path) -> Task:
    slug = path.stem
    if not _VALID_SLUG.match(slug):
        raise ValueError(
            f"task filename `{path.name}` contains characters not allowed "
            "in git branch names; use only letters, digits, hyphens, dots, "
            "and underscores"
        )
    text = path.read_text(encoding="utf-8")
    metadata_payload, body = _split_frontmatter(text)
    metadata = validate_task_metadata(metadata_payload)
    _validate_acceptance_criteria_for_status(path, metadata)
    return Task(path=path, slug=slug, metadata=metadata, body=body)


def list_tasks(directory: Path, *, git_repo: GitRepo | None = None) -> list[Task]:
    if not directory.exists():
        return []
    tasks: list[Task] = []
    enforce_append_only = directory.name in _PROMOTED_TASK_STATUSES
    for path in directory.glob("*.md"):
        try:
            if git_repo is not None and enforce_append_only:
                _ensure_append_only_promoted_task(path, git_repo)
            tasks.append(parse_task_file(path))
        except ValueError as exc:
            raise ValueError(f"malformed task file `{path.name}`: {exc}") from exc
    tasks.sort(key=lambda task: task.slug)
    return tasks


def _validate_acceptance_criteria_for_status(
    path: Path, metadata: TaskMetadata
) -> None:
    if path.parent.name not in _PROMOTED_TASK_STATUSES:
        return
    if metadata.acceptance_criteria:
        return
    raise ValueError("promoted tasks must include non-empty acceptance_criteria")


def _ensure_append_only_promoted_task(path: Path, git_repo: GitRepo) -> None:
    if git_repo.path_matches_head(path):
        return
    relative_path = git_repo.relative_path(path)
    raise ValueError(
        f"promoted task file `{relative_path}` was modified in place; "
        "create a follow-up draft task instead"
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


def validate_draft_promotion(
    tasks: list[Task],
    *,
    all_draft_slugs: set[str],
    promoted_slugs: set[str],
    promoted_deps: dict[str, list[str]] | None = None,
) -> None:
    if not tasks:
        raise ValueError("no draft tasks selected for promotion")

    selected_slugs = {task.slug for task in tasks}
    missing_criteria = sorted(
        task.slug for task in tasks if not task.metadata.acceptance_criteria
    )
    if missing_criteria:
        joined = ", ".join(missing_criteria)
        raise ValueError(
            "draft tasks must include non-empty acceptance_criteria before "
            f"promotion: {joined}"
        )

    collisions = sorted(selected_slugs & promoted_slugs)
    if collisions:
        joined = ", ".join(collisions)
        raise ValueError(f"draft tasks already exist in promoted states: {joined}")

    blocked_dependencies: list[str] = []
    unknown_dependencies: list[str] = []
    for task in tasks:
        for dependency in task.metadata.depends_on:
            if dependency in selected_slugs or dependency in promoted_slugs:
                continue
            if dependency in all_draft_slugs:
                blocked_dependencies.append(f"{task.slug} -> {dependency}")
                continue
            unknown_dependencies.append(f"{task.slug} -> {dependency}")
    if blocked_dependencies:
        joined = ", ".join(sorted(blocked_dependencies))
        raise ValueError(
            "draft promotion depends on draft tasks outside the promotion "
            f"batch: {joined}"
        )
    if unknown_dependencies:
        joined = ", ".join(sorted(unknown_dependencies))
        raise ValueError(f"draft promotion has unknown dependency references: {joined}")

    cycle = _detect_cycle(tasks, promoted_slugs, promoted_deps or {})
    if cycle:
        joined = " -> ".join(cycle)
        raise ValueError(
            f"draft promotion introduces a cyclic dependency: {joined}"
        )


def _detect_cycle(
    tasks: list[Task],
    promoted_slugs: set[str],
    promoted_deps: dict[str, list[str]],
) -> list[str] | None:
    """Return a cycle as a list of slugs, or None if the graph is acyclic."""
    graph: dict[str, list[str]] = {}
    for task in tasks:
        graph[task.slug] = list(task.metadata.depends_on)
    for slug in promoted_slugs:
        if slug not in graph:
            graph[slug] = promoted_deps.get(slug, [])

    visited: set[str] = set()
    in_stack: set[str] = set()
    path: list[str] = []

    def dfs(node: str) -> list[str] | None:
        visited.add(node)
        in_stack.add(node)
        path.append(node)
        for neighbor in graph.get(node, []):
            if neighbor not in graph:
                continue
            if neighbor in in_stack:
                cycle_start = path.index(neighbor)
                return path[cycle_start:] + [neighbor]
            if neighbor not in visited:
                result = dfs(neighbor)
                if result is not None:
                    return result
        path.pop()
        in_stack.discard(node)
        return None

    for node in graph:
        if node not in visited:
            result = dfs(node)
            if result is not None:
                return result
    return None


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

    boundary = _find_frontmatter_boundary(text)
    if boundary is None:
        raise ValueError("task file must end frontmatter with ---")

    metadata_text = text[4:boundary]
    body = text[boundary + len("---\n") :]
    if body.startswith("\n"):
        body = body[1:]

    loaded = _load_frontmatter(metadata_text)
    if not isinstance(loaded, dict):
        raise ValueError("task frontmatter must be an object")
    return cast(dict[str, object], loaded), body


def _find_frontmatter_boundary(text: str) -> int | None:
    try:
        events = yaml.parse(text)
        for event in events:
            if isinstance(event, DocumentEndEvent):
                boundary = event.start_mark.index
                if text.startswith("---\n", boundary):
                    return boundary
                return None
    except yaml.YAMLError:
        pass

    return _scan_frontmatter_boundary(text)


def _load_frontmatter(metadata_text: str) -> object:
    normalized = _normalize_frontmatter_plain_scalars(metadata_text)
    if normalized != metadata_text:
        try:
            return yaml.safe_load(normalized)
        except yaml.YAMLError:
            pass
    return yaml.safe_load(metadata_text)


def _scan_frontmatter_boundary(text: str) -> int | None:
    offset = 4
    block_scalar_indent: int | None = None

    while offset < len(text):
        line_end = text.find("\n", offset)
        if line_end == -1:
            line = text[offset:]
            next_offset = len(text)
        else:
            line = text[offset:line_end]
            next_offset = line_end + 1

        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))

        if block_scalar_indent is not None:
            if stripped and indent <= block_scalar_indent:
                block_scalar_indent = None
            else:
                offset = next_offset
                continue

        if line == "---":
            return offset

        _, separator, raw_value = line.partition(":")
        if separator and raw_value.strip().startswith(("|", ">")):
            block_scalar_indent = indent

        offset = next_offset

    return None


def _normalize_frontmatter_plain_scalars(metadata_text: str) -> str:
    lines = metadata_text.splitlines(keepends=True)
    normalized: list[str] = []
    list_key: str | None = None
    block_scalar_indent: int | None = None

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))

        if block_scalar_indent is not None:
            if stripped and indent <= block_scalar_indent:
                block_scalar_indent = None
            else:
                normalized.append(line)
                continue

        if indent == 0 and not line.startswith(("{", "[")):
            key, separator, raw_value = line.partition(":")
            if separator:
                value = _strip_inline_comment(raw_value.strip())
                list_key = key if not value else None
                if value.startswith(("|", ">")):
                    block_scalar_indent = indent
                    normalized.append(line)
                    continue
                if value and _should_quote_plain_scalar(value):
                    normalized.append(
                        f"{key}: {_quote_yaml_string(value)}{_line_ending(line)}"
                    )
                    continue

        if list_key is not None and line.startswith("  - "):
            value = _strip_inline_comment(line[4:].rstrip("\r\n"))
            if _should_quote_plain_scalar(value):
                normalized.append(
                    f"  - {_quote_yaml_string(value)}{_line_ending(line)}"
                )
                continue

        normalized.append(line)

    return "".join(normalized)


def _strip_inline_comment(value: str) -> str:
    if not value or value[0] in "\"'":
        return value
    idx = value.find(" #")
    if idx == -1:
        return value
    return value[:idx].rstrip()


def _should_quote_plain_scalar(value: str) -> bool:
    if not value:
        return False
    if value[0] in "\"'" or value[0] in "[{" or value[0] in "&*":
        return False
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value):
        return False
    if value.lower() in {"true", "false", "yes", "no", "on", "off", "null", "~"}:
        return False
    return True


def _quote_yaml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _format_error(path: Any, message: str) -> str:
    parts = [str(part) for part in path]
    if parts:
        return f"{'.'.join(parts)}: {message}"
    return message
