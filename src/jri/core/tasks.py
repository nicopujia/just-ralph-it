import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast

import yaml
from yaml.events import DocumentEndEvent

from .git import GitRepo
from .models import Task, TaskMetadata

_PROMOTED_TASK_STATUSES = frozenset({"todo", "doing", "done"})


def validate_task_metadata(payload: dict[str, object]) -> TaskMetadata:
    errors = _validate_task_metadata_payload(payload)
    if errors:
        raise ValueError(", ".join(errors))

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
    errors = _validate_state_payload(payload)
    if errors:
        raise ValueError(", ".join(errors))


def _validate_task_metadata_payload(payload: dict[str, object]) -> list[str]:
    errors: list[str] = []
    required = {"title", "priority", "assignee"}
    if "status" in payload:
        errors.append("unexpected key `status`: task status is determined by directory")
    for key in sorted(required - set(payload)):
        errors.append(f"`{key}` is required")
    title = payload.get("title")
    if "title" in payload and not isinstance(title, str):
        errors.append("`title` must be a string")
    elif isinstance(title, str) and len(title) > 75:
        errors.append("`title` must be 75 characters or fewer")
    priority = payload.get("priority")
    if "priority" in payload and (
        not isinstance(priority, int) or isinstance(priority, bool)
    ):
        errors.append("`priority` must be an integer")
    elif (
        isinstance(priority, int)
        and not isinstance(priority, bool)
        and not 0 <= priority <= 4
    ):
        errors.append("`priority` must be between 0 and 4")
    assignee = payload.get("assignee")
    if "assignee" in payload and assignee not in {"Ralph", "Human"}:
        errors.append("`assignee` must be one of Ralph, Human")
    _validate_string_list_field(payload, "depends_on", errors, unique=True)
    _validate_string_list_field(payload, "acceptance_criteria", errors, unique=False)
    return errors


def _validate_string_list_field(
    payload: dict[str, object], field_name: str, errors: list[str], *, unique: bool
) -> None:
    if field_name not in payload:
        return
    value = payload[field_name]
    if not isinstance(value, list):
        errors.append(f"`{field_name}` must be an array")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(f"`{field_name}[{index}]` must be a string")
    if unique and len(value) != len(set(value)):
        errors.append(f"`{field_name}` must contain unique items")


def _validate_state_payload(payload: dict[str, object]) -> list[str]:
    errors: list[str] = []
    allowed = {
        "started_at",
        "finished_at",
        "session",
        "branch",
        "current_task",
        "process",
        "active_attempt",
        "attempts",
    }
    for key in sorted(set(payload) - allowed):
        errors.append(f"unexpected key `{key}`")
    for key in ("started_at", "finished_at"):
        _validate_optional_int(payload, key, errors)
    for key in ("session", "branch", "current_task"):
        _validate_optional_str(payload, key, errors)
    _validate_process_payload(payload.get("process"), errors)
    _validate_attempt_field(payload, "active_attempt", errors)
    attempts = payload.get("attempts")
    if "attempts" in payload:
        if not isinstance(attempts, list):
            errors.append("`attempts` must be an array")
        else:
            for index, attempt in enumerate(attempts):
                _validate_attempt_payload(attempt, f"attempts[{index}]", errors)
    return errors


def _validate_optional_int(
    payload: dict[str, object], field_name: str, errors: list[str]
) -> None:
    value = payload.get(field_name)
    if field_name in payload and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        errors.append(f"`{field_name}` must be an integer")


def _validate_optional_str(
    payload: dict[str, object], field_name: str, errors: list[str]
) -> None:
    value = payload.get(field_name)
    if field_name in payload and not isinstance(value, str):
        errors.append(f"`{field_name}` must be a string")


def _validate_process_payload(value: object, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append("`process` must be an object")
        return
    process = cast(dict[str, object], value)
    allowed = {"loop_pid", "child_pid", "log_path", "detached"}
    for key in sorted(set(process) - allowed):
        errors.append(f"unexpected key `process.{key}`")
    for key in ("loop_pid", "child_pid"):
        field_value = process.get(key)
        if (
            key in process
            and field_value is not None
            and (not isinstance(field_value, int) or isinstance(field_value, bool))
        ):
            errors.append(f"`process.{key}` must be an integer or null")
    log_path = process.get("log_path")
    if "log_path" in process and log_path is not None and not isinstance(log_path, str):
        errors.append("`process.log_path` must be a string or null")
    detached = process.get("detached")
    if "detached" in process and not isinstance(detached, bool):
        errors.append("`process.detached` must be a boolean")


def _validate_attempt_field(
    payload: dict[str, object], field_name: str, errors: list[str]
) -> None:
    if field_name in payload:
        _validate_attempt_payload(payload[field_name], field_name, errors)


def _validate_attempt_payload(value: object, label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"`{label}` must be an object")
        return
    attempt = cast(dict[str, object], value)
    allowed = {
        "number",
        "task_slug",
        "branch",
        "started_at",
        "finished_at",
        "log_path",
        "session_id",
        "result",
        "result_payload",
    }
    required = {"number", "task_slug", "branch", "started_at"}
    for key in sorted(set(attempt) - allowed):
        errors.append(f"unexpected key `{label}.{key}`")
    for key in sorted(required - set(attempt)):
        errors.append(f"`{label}.{key}` is required")
    for key in ("number", "started_at", "finished_at"):
        field_value = attempt.get(key)
        if key in attempt and (
            not isinstance(field_value, int) or isinstance(field_value, bool)
        ):
            errors.append(f"`{label}.{key}` must be an integer")
    for key in ("task_slug", "branch", "log_path", "session_id"):
        field_value = attempt.get(key)
        if key in attempt and not isinstance(field_value, str):
            errors.append(f"`{label}.{key}` must be a string")
    result = attempt.get("result")
    if "result" in attempt and result not in {
        "completed",
        "incompleted",
        "incomplete",
        "needs_human",
        "failed",
        "interrupted",
        "timeout",
    }:
        errors.append(f"`{label}.result` must be a known attempt result")
    if "result_payload" in attempt:
        _validate_result_payload(
            attempt["result_payload"], f"{label}.result_payload", errors
        )


def _validate_result_payload(value: object, label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"`{label}` must be an object")
        return
    payload = cast(dict[str, object], value)
    allowed = {"result", "summary", "learnings", "blocker", "human_task"}
    for key in sorted(set(payload) - allowed):
        errors.append(f"unexpected key `{label}.{key}`")
    if payload.get("result") not in {"completed", "incompleted", "needs_human"}:
        errors.append(
            f"`{label}.result` must be one of completed, incompleted, needs_human"
        )
    for key in ("summary", "blocker"):
        field_value = payload.get(key)
        if key in payload and not isinstance(field_value, str):
            errors.append(f"`{label}.{key}` must be a string")
    learnings = payload.get("learnings")
    if "learnings" in payload:
        if not isinstance(learnings, list):
            errors.append(f"`{label}.learnings` must be an array")
        else:
            for index, item in enumerate(learnings):
                if not isinstance(item, str):
                    errors.append(f"`{label}.learnings[{index}]` must be a string")
    if "human_task" in payload:
        _validate_human_task_payload(
            payload["human_task"], f"{label}.human_task", errors
        )


def _validate_human_task_payload(value: object, label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append(f"`{label}` must be an object")
        return
    payload = cast(dict[str, object], value)
    allowed = {"title", "body", "acceptance_criteria", "priority"}
    for key in sorted(set(payload) - allowed):
        errors.append(f"unexpected key `{label}.{key}`")
    for key in ("title", "body"):
        field_value = payload.get(key)
        if key in payload and not isinstance(field_value, str):
            errors.append(f"`{label}.{key}` must be a string")
    _validate_string_list_field(payload, "acceptance_criteria", errors, unique=False)
    priority = payload.get("priority")
    if "priority" in payload and (
        not isinstance(priority, int) or isinstance(priority, bool)
    ):
        errors.append(f"`{label}.priority` must be an integer")


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
        "create a follow-up todo task instead"
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
    frontmatter = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False).strip()
    return "---\n" + frontmatter + "\n---\n\n" + task.body


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
        parse_yaml = cast(Callable[[str], Iterable[object]], yaml.parse)
        for event in parse_yaml(text):
            if isinstance(event, DocumentEndEvent):
                mark = event.start_mark
                if mark is None:
                    return None
                boundary = mark.index
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
