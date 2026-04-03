import json
import re
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import yaml
from jsonschema import Draft202012Validator
from yaml.events import DocumentEndEvent

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
    return Task(path=path, slug=slug, metadata=metadata, body=body)


def list_tasks(directory: Path) -> list[Task]:
    if not directory.exists():
        return []
    tasks: list[Task] = []
    for path in directory.glob("*.md"):
        try:
            tasks.append(parse_task_file(path))
        except ValueError as exc:
            raise ValueError(f"malformed task file `{path.name}`: {exc}") from exc
    tasks.sort(key=lambda task: task.slug)
    return tasks


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
