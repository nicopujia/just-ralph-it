import argparse
import json
import sys
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator, SchemaError

from jri.core.tasks import list_tasks, validate_state_payload

_TASK_STATUSES = ("draft", "todo", "doing", "done")


def validate_repo(root: Path) -> None:
    validate_packaged_schemas()
    validate_task_tree(root)
    validate_state_file(root)


def validate_packaged_schemas() -> None:
    errors: list[str] = []
    schema_paths = sorted(files("jri.core.schemas").iterdir(), key=lambda p: p.name)
    for schema_path in schema_paths:
        if not schema_path.name.endswith(".json"):
            continue
        try:
            payload = json.loads(schema_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"schema `{schema_path.name}` is not valid JSON: {exc}")
            continue
        try:
            Draft202012Validator.check_schema(payload)
        except SchemaError as exc:
            errors.append(
                f"schema `{schema_path.name}` is not a valid JSON Schema: {exc}"
            )
    if errors:
        raise ValueError("; ".join(errors))


def validate_task_tree(root: Path) -> None:
    task_root = root / ".jri" / "tasks"
    errors: list[str] = []
    for status in _TASK_STATUSES:
        try:
            list_tasks(task_root / status)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("; ".join(errors))


def validate_state_file(root: Path) -> None:
    state_path = root / ".jri" / "state.json"
    if not state_path.exists():
        return

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"state.json is corrupted: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("state.json must contain an object")
    try:
        validate_state_payload(payload)
    except ValueError as exc:
        raise ValueError(f"state.json has invalid content: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate repo JSON schemas and JRI files."
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=Path.cwd(),
        type=Path,
        help="repository root to validate",
    )
    args = parser.parse_args(argv)

    try:
        validate_repo(args.root)
    except ValueError as exc:
        print(f"schema check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
