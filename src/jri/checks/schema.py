import argparse
import json
import sys
from pathlib import Path
from typing import cast

from jri.core.git import GitRepo
from jri.core.graph import validate_graph_tree
from jri.core.tasks import list_tasks, validate_state_payload

_TASK_STATUSES = ("todo", "doing", "done")


def validate_repo(root: Path) -> None:
    validate_task_tree(root)
    validate_state_file(root)
    validate_graph_tree(root)


def validate_task_tree(root: Path) -> None:
    task_root = root / ".jri" / "tasks"
    errors: list[str] = []
    git_repo = GitRepo(root)
    repo_for_tasks = git_repo if git_repo.is_repo() else None
    for status in _TASK_STATUSES:
        try:
            list_tasks(task_root / status, git_repo=repo_for_tasks)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("; ".join(errors))


def validate_state_file(root: Path) -> None:
    state_path = root / ".jri" / "state.json"
    if not state_path.exists():
        return

    try:
        payload: object = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"state.json is corrupted: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("state.json must contain an object")
    try:
        validate_state_payload(cast(dict[str, object], payload))
    except ValueError as exc:
        raise ValueError(f"state.json has invalid content: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate JRI managed files.")
    parser.add_argument("root", nargs="?", default=Path.cwd(), type=Path, help="repository root to validate")
    args = parser.parse_args(argv)

    try:
        validate_repo(args.root)
    except ValueError as exc:
        print(f"schema check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
