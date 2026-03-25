"""YAML-based task tracking."""

import logging
import shutil
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

STATUSES = ("todo", "doing", "done", "draft")


def _yaml_dump(data: dict) -> str:
    """Dump YAML using block scalars (|) for multiline strings."""
    def str_representer(dumper, s):
        style = "|" if "\n" in s else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", s, style=style)

    dumper = yaml.Dumper
    dumper.add_representer(str, str_representer)
    return yaml.dump(data, Dumper=dumper, allow_unicode=True, sort_keys=False)


def tasks_dir(project_dir: str) -> Path:
    return Path(project_dir) / ".ralph" / "tasks"


def init_tasks(project_dir: str) -> None:
    base = tasks_dir(project_dir)
    for status in STATUSES:
        d = base / status
        d.mkdir(parents=True, exist_ok=True)
        gitkeep = d / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.touch()


def _parse_task(path: Path) -> dict:
    data = yaml.safe_load(path.read_text()) or {}
    if not data.get("title"):
        logger.warning("Task %s missing title", path.name)
    data["id"] = path.stem
    data["status"] = path.parent.name
    data.pop("type", None)  # type field dropped
    data.setdefault("priority", 4)
    data.setdefault("depends_on", [])
    # Ensure acceptance_criteria is always a list
    ac = data.get("acceptance_criteria", [])
    if isinstance(ac, str):
        data["acceptance_criteria"] = [line.strip() for line in ac.split("\n") if line.strip()]
    return data


def _find_task_path(project_dir: str, slug: str) -> Path | None:
    base = tasks_dir(project_dir)
    for status in STATUSES:
        path = base / status / f"{slug}.yaml"
        if path.exists():
            return path
    return None


def list_all(project_dir: str) -> list[dict]:
    base = tasks_dir(project_dir)
    results = []
    for status in STATUSES:
        status_dir = base / status
        if not status_dir.exists():
            continue
        for path in sorted(status_dir.glob("*.yaml")):
            try:
                results.append(_parse_task(path))
            except Exception:
                logger.exception("Failed to parse task %s", path)
    return results


def get_task(project_dir: str, slug: str) -> dict | None:
    path = _find_task_path(project_dir, slug)
    if path is None:
        return None
    return _parse_task(path)


def get_ready(project_dir: str) -> list[dict]:
    all_tasks = list_all(project_dir)
    done_slugs = {t["id"] for t in all_tasks if t["status"] == "done"}
    ready = [
        t for t in all_tasks
        if t["status"] == "todo"
        and all(dep in done_slugs for dep in t.get("depends_on", []))
    ]
    ready.sort(key=lambda t: t.get("priority", 4))
    return ready


def set_status(project_dir: str, slug: str, new_status: str) -> None:
    if new_status not in STATUSES:
        raise ValueError(f"Invalid status: {new_status}")
    path = _find_task_path(project_dir, slug)
    if path is None:
        raise FileNotFoundError(f"Task not found: {slug}")
    if path.parent.name == new_status:
        return
    dest = tasks_dir(project_dir) / new_status / path.name
    shutil.move(str(path), str(dest))


def update_field(project_dir: str, slug: str, **fields) -> None:
    path = _find_task_path(project_dir, slug)
    if path is None:
        raise FileNotFoundError(f"Task not found: {slug}")
    data = yaml.safe_load(path.read_text()) or {}
    data.update(fields)
    path.write_text(_yaml_dump(data))


def create_task(project_dir: str, slug: str, data: dict) -> None:
    dest = tasks_dir(project_dir) / "todo" / f"{slug}.yaml"
    if dest.exists():
        raise FileExistsError(f"Task already exists: {slug}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_yaml_dump(data))


def task_count(project_dir: str) -> int:
    base = tasks_dir(project_dir)
    count = 0
    for status in STATUSES:
        status_dir = base / status
        if status_dir.exists():
            count += len(list(status_dir.glob("*.yaml")))
    return count
