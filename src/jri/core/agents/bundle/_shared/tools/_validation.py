import json
import re
import sys
from difflib import unified_diff
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

import yaml

from .....tasks import validate_task_metadata

if TYPE_CHECKING:
    from .....service import JriService

SLUG_RE = re.compile(r"^[a-zA-Z0-9][-a-zA-Z0-9_.]*$")


class ExactEdit(TypedDict):
    oldText: str
    newText: str


def load_payload() -> dict[str, object]:
    try:
        payload: object = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("tool payload must be a JSON object")
    return cast(dict[str, object], payload)


def print_result(message: str) -> None:
    sys.stdout.write(message)


def assert_slug(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{name}` must be a non-empty string")
    slug = value.strip()
    if not SLUG_RE.match(slug):
        raise ValueError(
            f"`{name}` contains characters not allowed in task filenames; "
            "use only letters, digits, hyphens, dots, and underscores"
        )
    return slug


def assert_string_list(name: str, value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"`{name}` must be a list of non-empty strings")
    items = cast(list[object], value)
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise ValueError(f"`{name}` must be a list of non-empty strings")
    strings = cast(list[str], items)
    if len(set(strings)) != len(strings):
        raise ValueError(f"`{name}` must not contain duplicates")
    return strings


def assert_slug_list(name: str, value: object) -> list[str] | None:
    items = assert_string_list(name, value)
    if items is None:
        return None
    return [assert_slug(name, item) for item in items]


def ensure_expected_real_path(parent_dir: Path, child_name: str) -> Path:
    child_path = parent_dir / child_name
    child_path.mkdir(parents=True, exist_ok=True)
    real_child_path = child_path.resolve()
    if real_child_path != child_path:
        raise ValueError("refusing to write outside `.jri/tasks/`")
    return child_path


def ensure_task_path_within(directory: Path, slug: str) -> Path:
    task_path = (directory / f"{slug}.md").resolve()
    try:
        task_path.relative_to(directory)
    except ValueError as exc:
        raise ValueError("refusing to write outside `.jri/tasks/`") from exc
    return task_path


def read_task(task_path: Path) -> tuple[dict[str, object], str]:
    source = task_path.read_text(encoding="utf-8")
    return read_task_source(task_path, source)


def read_task_source(task_path: Path, source: str) -> tuple[dict[str, object], str]:
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
        metadata: object = yaml.safe_load(metadata_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid task metadata YAML: {task_path}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"invalid task metadata object: {task_path}")
    metadata_payload = cast(dict[str, object], metadata)
    validate_task_metadata(metadata_payload)
    depends_on = metadata_payload.get("depends_on")
    if depends_on is not None:
        assert_string_list("depends_on", depends_on)
    acceptance_criteria = metadata_payload.get("acceptance_criteria")
    if acceptance_criteria is not None:
        assert_string_list("acceptance_criteria", acceptance_criteria)
    return metadata_payload, body


def serialize_task(metadata: dict[str, object], body: str) -> str:
    frontmatter = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=False).strip()
    return f"---\n{frontmatter}\n---\n\n{body}"


def assert_exact_edits(payload: dict[str, object]) -> list[ExactEdit]:
    edits = payload.get("edits")
    if not isinstance(edits, list) or not edits:
        raise ValueError("`edits` must be a non-empty list")
    normalized: list[ExactEdit] = []
    for index, edit in enumerate(cast(list[object], edits), start=1):
        if not isinstance(edit, dict):
            raise ValueError(f"`edits[{index}]` must be an object")
        edit_payload = cast(dict[str, object], edit)
        old_text = edit_payload.get("oldText")
        new_text = edit_payload.get("newText")
        if not isinstance(old_text, str) or not old_text:
            raise ValueError(f"`edits[{index}].oldText` must be a non-empty string")
        if not isinstance(new_text, str):
            raise ValueError(f"`edits[{index}].newText` must be a string")
        normalized.append({"oldText": old_text, "newText": new_text})
    return normalized


def apply_exact_edits(source: str, edits: list[ExactEdit]) -> tuple[str, int]:
    updated = source
    replacements = 0
    for index, edit in enumerate(edits, start=1):
        old_text = edit["oldText"]
        count = updated.count(old_text)
        if count == 0:
            raise ValueError(f"`edits[{index}].oldText` was not found")
        if count > 1:
            raise ValueError(f"`edits[{index}].oldText` matched {count} blocks; make it unique")
        updated = updated.replace(old_text, edit["newText"], 1)
        replacements += 1
    return updated, replacements


def diff_text(path_label: str, before: str, after: str) -> str:
    return "".join(
        unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path_label}",
            tofile=f"b/{path_label}",
        )
    )


def repo_root() -> Path:
    return Path.cwd().resolve()


def repo_root_child(name: str) -> Path:
    root = repo_root()
    path = root / name
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError(f"refusing to access outside repo root: {name}") from exc
    return path


def task_dirs(root: Path) -> tuple[Path, Path, Path]:
    repo_root_path = root.resolve()
    jri_dir = ensure_expected_real_path(repo_root_path, ".jri")
    tasks_dir = ensure_expected_real_path(jri_dir, "tasks")
    todo_dir = ensure_expected_real_path(tasks_dir, "todo")
    doing_dir = ensure_expected_real_path(tasks_dir, "doing")
    done_dir = ensure_expected_real_path(tasks_dir, "done")
    return todo_dir, doing_dir, done_dir


def slugify(title: str) -> str:
    slug = title.strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    slug = re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", slug)
    if not slug:
        raise ValueError("could not derive a valid slug from title; pass `slug`")
    return slug


def service(root: Path) -> "JriService":
    package = sys.modules.get("jri.core.agents.bundle._shared.tools")
    service_type = getattr(package, "JriService", None) if package is not None else None
    if service_type is None:
        from .....service import JriService

        service_type = JriService
    return cast("type[JriService]", service_type)(root)
