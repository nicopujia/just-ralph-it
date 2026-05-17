"""Integration self-hosting proof for a JRI-shaped repository.

Demonstrates the full JRI lifecycle - idea to executable todo tasks to
loop - against a repository that mirrors this project's own structure. Three
todo tasks with a dependency chain are committed, then the Ralph loop executes
them all without manual intervention.

Run with:

    uv run pytest tests/integration/test_self_hosting_proof.py
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from re import search
from typing import cast

from jri.core.models import AgentRunResult, RalphResultPayload
from jri.core.service import JriService
from tests.conftest import run_cli
from tests.helpers import git, read_json, write_task

# -- Per-task file implementations the fake Ralph agent "writes" ---------------

_TASK_ARTIFACTS: dict[str, tuple[str, str]] = {
    "implement-greet": ("src/greet.py", 'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n'),
    "add-greet-tests": (
        "tests/test_greet.py",
        "from greet import greet\n\n\ndef test_greet():\n    assert greet('world') == 'Hello, world!'\n",
    ),
    "update-changelog": ("CHANGELOG.md", "# Changelog\n\n## 0.1.0\n\n- Initial release.\n"),
}


class _SelfHostingFakeClient:
    """Simulates Ralph completing tasks on a JRI-shaped repository."""

    def __init__(self) -> None:
        self.model: str | None = None

    def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
        return []

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: Callable[[int], None] | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        slug = _extract_slug(prompt)
        if slug in _TASK_ARTIFACTS:
            relative_path, content = _TASK_ARTIFACTS[slug]
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return AgentRunResult(
            returncode=0,
            result="completed",
            session_id=f"ses_proof_{slug}",
            payload=RalphResultPayload(result="completed", summary=f"Completed {slug}."),
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("{}", encoding="utf-8")


def _extract_slug(prompt: str) -> str:
    """Extract the task slug from the standard Ralph prompt."""
    match = search(r"\.jri/tasks/doing/([^/]+)\.md", prompt)
    return match.group(1) if match else ""


# -- Test ----------------------------------------------------------------------


def test_self_hosting_proof_idea_to_convergence(git_repo: Path) -> None:
    # Phase 1: scaffold a repo that mirrors this project's shape
    _setup_project_structure(git_repo)

    # Phase 2: idea -> executable todo task chain
    _create_executable_todo_tasks(git_repo)

    # Phase 3: loop (the runtime executes the full task pipeline)
    client = _SelfHostingFakeClient()
    service = JriService(git_repo, agent_runtime=client)
    completed = service.start()

    # Phase 4: assert convergence
    _assert_convergence(git_repo, completed)


# -- Helpers -------------------------------------------------------------------


def _setup_project_structure(repo: Path) -> None:
    """Create a repo that mirrors this project's structure."""
    assert run_cli(["init"], cwd=repo) == 0

    (repo / "pyproject.toml").write_text("[project]\nname = 'proof'\nversion = '0.1.0'\n", encoding="utf-8")
    (repo / "Makefile").write_text(".PHONY: check\ncheck:\n\t@true\n", encoding="utf-8")
    src_dir = repo / "src"
    src_dir.mkdir()
    (src_dir / "__init__.py").write_text("", encoding="utf-8")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")

    git(repo, "add", "pyproject.toml", "Makefile", "src/", "tests/")
    git(repo, "commit", "-m", "add project structure")


_TASK_SPECS: list[tuple[str, str, int, list[str], str, list[str]]] = [
    (
        "implement-greet",
        "Implement greet module",
        0,
        [],
        "Create src/greet.py with a greet(name) -> str function.",
        ["File src/greet.py exists and contains a greet function."],
    ),
    (
        "add-greet-tests",
        "Add greet tests",
        1,
        ["implement-greet"],
        "Create tests/test_greet.py with tests for greet().",
        ["File tests/test_greet.py exists with at least one test."],
    ),
    (
        "update-changelog",
        "Update changelog",
        2,
        ["add-greet-tests"],
        "Create CHANGELOG.md with an initial entry.",
        ["File CHANGELOG.md exists at the repo root."],
    ),
]


def _create_executable_todo_tasks(repo: Path) -> None:
    """Simulate compilation into executable todo tasks."""
    for slug, title, priority, depends_on, body, criteria in _TASK_SPECS:
        write_task(
            repo,
            status="todo",
            slug=slug,
            title=title,
            priority=priority,
            assignee="Ralph",
            body=body,
            depends_on=depends_on,
            acceptance_criteria=criteria,
        )
    git(repo, "add", ".jri/tasks/todo/")
    git(repo, "commit", "-m", "add todo tasks")


def _assert_convergence(repo: Path, completed: int) -> None:
    """Verify the loop converged with local reset state and no JRI tags."""
    # All three tasks completed
    assert completed == 3, f"expected 3 completed tasks, got {completed}"

    # Every task landed in done/
    for slug, _title, _pri, _deps, _body, _criteria in _TASK_SPECS:
        done_path = repo / ".jri" / "tasks" / "done" / f"{slug}.md"
        assert done_path.exists(), f"task {slug} should be in done/"

    # No tasks remain in todo/
    todo_files = [p for p in (repo / ".jri" / "tasks" / "todo").glob("*.md") if p.name != ".gitkeep"]
    assert todo_files == [], f"unexpected todo tasks: {todo_files}"

    assert [tag for tag in git(repo, "tag").splitlines() if tag.startswith("jri/")] == []

    # State has attempts recorded
    state = read_json(repo / ".jri" / "state.json")
    attempts = cast(list[dict[str, object]], state.get("attempts", []))
    assert len(attempts) == 3, f"expected 3 attempts, got {len(attempts)}"
    reset_points = cast(dict[str, dict[str, object]], state.get("reset_points", {}))
    assert set(reset_points.get("main", {})) == {slug for slug, _title, _pri, _deps, _body, _criteria in _TASK_SPECS}

    # Artifacts from each task exist on disk
    assert (repo / "src" / "greet.py").exists()
    assert (repo / "tests" / "test_greet.py").exists()
    assert (repo / "CHANGELOG.md").exists()

    # Git history shows clean task completions
    log = git(repo, "log", "--oneline").splitlines()
    completion_commits = [line for line in log if "jri: complete" in line]
    assert len(completion_commits) == 3
