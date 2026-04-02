import json
import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_task(
    repo: Path,
    *,
    status: str,
    slug: str,
    title: str,
    priority: int,
    assignee: str,
    body: str,
    depends_on: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
) -> Path:
    depends_on = depends_on or []
    acceptance_criteria = acceptance_criteria or []
    task_path = repo / ".jri" / "tasks" / status / f"{slug}.md"
    metadata = {
        "title": title,
        "priority": priority,
        "assignee": assignee,
        "depends_on": depends_on,
        "acceptance_criteria": acceptance_criteria,
    }
    task_path.write_text(
        "---\n" + json.dumps(metadata, indent=2) + "\n---\n\n" + body,
        encoding="utf-8",
    )
    return task_path
