"""YAML-based issue tracking."""

import logging
import shutil
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

STATUSES = ("open", "closed", "deferred")


def _yaml_dump(data: dict) -> str:
    """Dump YAML using block scalars (|) for multiline strings."""
    def str_representer(dumper, s):
        style = "|" if "\n" in s else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", s, style=style)

    dumper = yaml.Dumper
    dumper.add_representer(str, str_representer)
    return yaml.dump(data, Dumper=dumper, allow_unicode=True, sort_keys=False)


def issues_dir(project_dir: str) -> Path:
    return Path(project_dir) / ".ralph" / "issues"


def init_issues(project_dir: str) -> None:
    base = issues_dir(project_dir)
    for status in STATUSES:
        (base / status).mkdir(parents=True, exist_ok=True)


def _parse_issue(path: Path) -> dict:
    data = yaml.safe_load(path.read_text()) or {}
    if not data.get("title"):
        logger.warning("Issue %s missing title", path.name)
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


def _find_issue_path(project_dir: str, slug: str) -> Path | None:
    base = issues_dir(project_dir)
    for status in STATUSES:
        path = base / status / f"{slug}.yaml"
        if path.exists():
            return path
    return None


def list_all(project_dir: str) -> list[dict]:
    base = issues_dir(project_dir)
    results = []
    for status in STATUSES:
        status_dir = base / status
        if not status_dir.exists():
            continue
        for path in sorted(status_dir.glob("*.yaml")):
            try:
                results.append(_parse_issue(path))
            except Exception:
                logger.exception("Failed to parse issue %s", path)
    return results


def get_issue(project_dir: str, slug: str) -> dict | None:
    path = _find_issue_path(project_dir, slug)
    if path is None:
        return None
    return _parse_issue(path)


def get_ready(project_dir: str) -> list[dict]:
    all_issues = list_all(project_dir)
    closed_slugs = {i["id"] for i in all_issues if i["status"] == "closed"}
    ready = [
        i for i in all_issues
        if i["status"] == "open"
        and all(dep in closed_slugs for dep in i.get("depends_on", []))
    ]
    ready.sort(key=lambda i: i.get("priority", 4))
    return ready


def set_status(project_dir: str, slug: str, new_status: str) -> None:
    if new_status not in STATUSES:
        raise ValueError(f"Invalid status: {new_status}")
    path = _find_issue_path(project_dir, slug)
    if path is None:
        raise FileNotFoundError(f"Issue not found: {slug}")
    if path.parent.name == new_status:
        return
    dest = issues_dir(project_dir) / new_status / path.name
    shutil.move(str(path), str(dest))


def update_field(project_dir: str, slug: str, **fields) -> None:
    path = _find_issue_path(project_dir, slug)
    if path is None:
        raise FileNotFoundError(f"Issue not found: {slug}")
    data = yaml.safe_load(path.read_text()) or {}
    data.update(fields)
    path.write_text(_yaml_dump(data))


def create_issue(project_dir: str, slug: str, data: dict) -> None:
    dest = issues_dir(project_dir) / "open" / f"{slug}.yaml"
    if dest.exists():
        raise FileExistsError(f"Issue already exists: {slug}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_yaml_dump(data))


def issue_count(project_dir: str) -> int:
    base = issues_dir(project_dir)
    count = 0
    for status in STATUSES:
        status_dir = base / status
        if status_dir.exists():
            count += len(list(status_dir.glob("*.yaml")))
    return count
