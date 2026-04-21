import subprocess
from pathlib import Path
from typing import cast

import pytest

from jri.core.service import JriService
from jri.core.tasks import list_tasks, parse_task_file
from tests.conftest import LiveStartModels, run_cli
from tests.helpers import git, read_json, write_live_makefile, write_task

pytestmark = pytest.mark.live

_LIVE_TASK_TIMEOUT_SECONDS = 300


def _skip_unless_live(run_live_opencode: bool) -> None:
    if not run_live_opencode:
        pytest.skip("pass --run-live-opencode to enable live OpenCode tests")


def _init_live_repo(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_live_makefile(git_repo)
    git(git_repo, "add", "Makefile")
    git(git_repo, "commit", "-m", "configure live make check")


def test_start_with_real_opencode_completes_trivial_task(
    git_repo: Path,
    run_live_opencode: bool,
    live_start_models: LiveStartModels,
) -> None:
    _skip_unless_live(run_live_opencode)

    _init_live_repo(git_repo)
    write_task(
        git_repo,
        status="todo",
        slug="live-proof",
        title="Create live proof",
        priority=0,
        assignee="Ralph",
        body=(
            "Create a file named live-proof.txt at the repository root. "
            "Its exact contents must be `live test ok` followed by a newline."
        ),
        acceptance_criteria=[
            "A file named live-proof.txt exists at the repository root.",
            "Its exact contents are `live test ok` followed by a newline.",
        ],
    )
    git(git_repo, "add", ".jri/tasks/todo/live-proof.md")
    git(git_repo, "commit", "-m", "add live task")

    service = JriService(git_repo)

    completed = service.start(
        max_tasks=1,
        task_timeout=_LIVE_TASK_TIMEOUT_SECONDS,
        **live_start_models,
    )

    assert completed == 1
    assert (git_repo / "live-proof.txt").read_text(encoding="utf-8") == "live test ok\n"
    assert (git_repo / ".jri" / "tasks" / "done" / "live-proof.md").exists()
    tags = git(git_repo, "tag").splitlines()
    assert "jri/begin/live-proof" in tags
    assert "jri/end/live-proof" in tags


def test_start_with_real_opencode_completes_setup_task(
    git_repo: Path,
    run_live_opencode: bool,
    live_start_models: LiveStartModels,
) -> None:
    _skip_unless_live(run_live_opencode)

    assert run_cli(["init"], cwd=git_repo) == 0
    write_task(
        git_repo,
        status="todo",
        slug="setup-quality-entrypoint",
        title="Setup quality entrypoint",
        priority=0,
        assignee="Ralph",
        body=(
            "This is the greenfield setup task for this repository. Replace the "
            "placeholder `Makefile` check target created by `jri init` with a "
            "working bootstrap quality entrypoint. The resulting `make check` "
            "must exit successfully in the current repository, and when "
            "pytest-style tests exist under `tests/`, it must run them with "
            "`PYTHONPATH=src python -m pytest -q tests`. Keep the setup minimal "
            "and do not add application code."
        ),
        acceptance_criteria=[
            "`Makefile` no longer contains the placeholder "
            "`make check is not configured yet` message.",
            "Running `make check` at the repository root exits successfully.",
            "The `check` target runs `PYTHONPATH=src python -m pytest -q tests` "
            "when pytest-style tests exist under `tests/`.",
        ],
    )
    git(git_repo, "add", ".jri/tasks/todo/setup-quality-entrypoint.md")
    git(git_repo, "commit", "-m", "add live setup task")

    service = JriService(git_repo)

    completed = service.start(
        max_tasks=1,
        task_timeout=_LIVE_TASK_TIMEOUT_SECONDS,
        **live_start_models,
    )

    assert completed == 1
    makefile_text = (git_repo / "Makefile").read_text(encoding="utf-8")
    assert "make check is not configured yet" not in makefile_text
    assert "PYTHONPATH=src python -m pytest -q tests" in makefile_text
    check = subprocess.run(
        ["make", "check"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert (
        git_repo / ".jri" / "tasks" / "done" / "setup-quality-entrypoint.md"
    ).exists()
    tags = git(git_repo, "tag").splitlines()
    assert "jri/begin/setup-quality-entrypoint" in tags
    assert "jri/end/setup-quality-entrypoint" in tags
    state = read_json(git_repo / ".jri" / "state.json")
    attempts = cast(list[dict[str, object]], state["attempts"])
    assert len(attempts) == 1
    assert attempts[0]["result"] == "completed"


def test_start_with_real_opencode_completes_dependency_chain(
    git_repo: Path,
    run_live_opencode: bool,
    live_start_models: LiveStartModels,
) -> None:
    _skip_unless_live(run_live_opencode)

    _init_live_repo(git_repo)
    write_task(
        git_repo,
        status="todo",
        slug="create-greet-module",
        title="Create greet module",
        priority=0,
        assignee="Ralph",
        body=(
            "Create `src/greet.py` with a function `greet(name: str) -> str` "
            "that returns exactly `Hello, {name}!`."
        ),
        acceptance_criteria=[
            "A file named `src/greet.py` exists.",
            "It defines `greet(name: str) -> str`.",
            "Calling `greet('world')` returns `Hello, world!`.",
        ],
    )
    write_task(
        git_repo,
        status="todo",
        slug="add-greet-test",
        title="Add greet test",
        priority=1,
        assignee="Ralph",
        depends_on=["create-greet-module"],
        body=(
            "Create `tests/test_greet.py` with at least one pytest test for "
            "`greet('world') == 'Hello, world!'`."
        ),
        acceptance_criteria=[
            "A file named `tests/test_greet.py` exists.",
            "It contains at least one pytest test for `greet`.",
        ],
    )
    write_task(
        git_repo,
        status="todo",
        slug="document-greet-feature",
        title="Document greet feature",
        priority=2,
        assignee="Ralph",
        depends_on=["add-greet-test"],
        body=(
            "Create `CHANGELOG.md` at the repository root with a short entry "
            "mentioning the new greet feature."
        ),
        acceptance_criteria=[
            "A file named `CHANGELOG.md` exists at the repository root.",
            "It mentions the greet feature.",
        ],
    )
    git(git_repo, "add", ".jri/tasks/todo/")
    git(git_repo, "commit", "-m", "add dependency chain live tasks")

    service = JriService(git_repo)

    completed = service.start(
        task_timeout=_LIVE_TASK_TIMEOUT_SECONDS,
        **live_start_models,
    )

    assert completed == 3
    assert (git_repo / "src" / "greet.py").read_text(encoding="utf-8")
    assert (git_repo / "tests" / "test_greet.py").read_text(encoding="utf-8")
    changelog_text = (git_repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "greet" in changelog_text.lower()
    for slug in (
        "create-greet-module",
        "add-greet-test",
        "document-greet-feature",
    ):
        assert (git_repo / ".jri" / "tasks" / "done" / f"{slug}.md").exists()
    todo_tasks = [
        path
        for path in (git_repo / ".jri" / "tasks" / "todo").glob("*.md")
        if path.name != ".gitkeep"
    ]
    assert todo_tasks == []
    tags = git(git_repo, "tag").splitlines()
    for slug in (
        "create-greet-module",
        "add-greet-test",
        "document-greet-feature",
    ):
        assert f"jri/begin/{slug}" in tags
        assert f"jri/end/{slug}" in tags
    state = read_json(git_repo / ".jri" / "state.json")
    attempts = cast(list[dict[str, object]], state["attempts"])
    assert len(attempts) == 3
    assert [cast(str, attempt["result"]) for attempt in attempts] == [
        "completed",
        "completed",
        "completed",
    ]


def test_start_with_real_opencode_escalates_needs_human_task(
    git_repo: Path,
    run_live_opencode: bool,
    live_start_models: LiveStartModels,
) -> None:
    _skip_unless_live(run_live_opencode)

    _init_live_repo(git_repo)
    write_task(
        git_repo,
        status="todo",
        slug="obtain-production-secret",
        title="Obtain production secret",
        priority=0,
        assignee="Ralph",
        body=(
            "Create a file named `production-secret.txt` at the repository root "
            "containing the exact production API key used by the company. "
            "The key is intentionally not stored anywhere in this repository, "
            "in git history, or in the available tools. Do not invent, guess, "
            "or substitute a fake value. If the exact key is unavailable, you "
            "must report `needs_human` and request the missing secret from a human."
        ),
        acceptance_criteria=[
            "If the exact production API key is unavailable, Ralph reports "
            "`needs_human` instead of guessing.",
            "No invented or placeholder secret is written to `production-secret.txt`.",
            "A Human follow-up task is created requesting the missing secret.",
        ],
    )
    git(git_repo, "add", ".jri/tasks/todo/obtain-production-secret.md")
    git(git_repo, "commit", "-m", "add needs human live task")

    service = JriService(git_repo)

    completed = service.start(
        max_tasks=1,
        task_timeout=_LIVE_TASK_TIMEOUT_SECONDS,
        **live_start_models,
    )

    assert completed == 0
    assert not (git_repo / "production-secret.txt").exists()
    assert (
        git_repo / ".jri" / "tasks" / "todo" / "obtain-production-secret.md"
    ).exists()
    assert not (
        git_repo / ".jri" / "tasks" / "doing" / "obtain-production-secret.md"
    ).exists()
    assert not (
        git_repo / ".jri" / "tasks" / "done" / "obtain-production-secret.md"
    ).exists()

    todo_tasks = list_tasks(git_repo / ".jri" / "tasks" / "todo")
    original_task = parse_task_file(
        git_repo / ".jri" / "tasks" / "todo" / "obtain-production-secret.md"
    )
    human_tasks = [task for task in todo_tasks if task.metadata.assignee == "Human"]

    assert len(human_tasks) == 1
    human_task = human_tasks[0]
    assert original_task.metadata.depends_on == [human_task.slug]
    assert human_task.metadata.acceptance_criteria
    assert "## Blocker" in human_task.body
    assert "production" in human_task.body.lower()
    assert "secret" in human_task.body.lower()
    assert "obtain-production-secret" in human_task.body

    state = read_json(git_repo / ".jri" / "state.json")
    attempts = cast(list[dict[str, object]], state["attempts"])
    assert len(attempts) == 1
    assert attempts[0]["result"] == "needs_human"

    exported_sessions = list(
        (git_repo / ".jri" / "logs" / "external" / "opencode").glob("*.json")
    )
    assert exported_sessions
