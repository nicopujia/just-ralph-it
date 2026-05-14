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


def capture_worktree_state(repo: Path) -> dict[str, str]:
    symbolic_ref = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "head": git(repo, "rev-parse", "HEAD"),
        "branch": git(repo, "branch", "--show-current"),
        "symbolic_ref": symbolic_ref.stdout.strip(),
        "status": git(repo, "status", "--porcelain=v1"),
        "cached_diff": git(repo, "diff", "--cached", "--name-status"),
        "worktree_diff": git(repo, "diff", "--name-status"),
    }


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_passing_makefile(repo: Path) -> None:
    (repo / "Makefile").write_text(
        ".PHONY: check\n\ncheck:\n\t@true\n",
        encoding="utf-8",
    )


def write_live_makefile(repo: Path) -> None:
    (repo / "Makefile").write_text(
        ".PHONY: check\n\n"
        "check:\n"
        "\t@set -- tests/test_*.py tests/*_test.py; \\\n"
        '\tfor path in "$$@"; do \\\n'
        '\t\tif [ -f "$$path" ]; then \\\n'
        "\t\t\tPYTHONPATH=src python -m pytest -q tests; exit $$?; \\\n"
        "\t\tfi; \\\n"
        "\tdone; \\\n"
        "\ttrue\n",
        encoding="utf-8",
    )


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
    task_path = repo / ".jri" / "tasks" / status / f"{slug}.md"
    metadata = {
        "title": title,
        "priority": priority,
        "assignee": assignee,
        "depends_on": depends_on,
    }
    if acceptance_criteria is not None:
        metadata["acceptance_criteria"] = acceptance_criteria
    elif status in {"todo", "doing", "done"}:
        metadata["acceptance_criteria"] = ["task completion is verifiable"]
    task_path.write_text(
        "---\n" + json.dumps(metadata, indent=2) + "\n---\n\n" + body,
        encoding="utf-8",
    )
    return task_path
