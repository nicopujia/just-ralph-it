from pathlib import Path

import pytest

from jri.core.service import JriService
from tests.conftest import run_cli
from tests.helpers import git, write_task

pytestmark = pytest.mark.live

_LIVE_TASK_TIMEOUT_SECONDS = 300


def test_start_with_real_opencode_completes_trivial_task(
    git_repo: Path,
    run_live_opencode: bool,
    opencode_model: str,
) -> None:
    if not run_live_opencode:
        pytest.skip("pass --run-live-opencode to enable live OpenCode tests")

    assert run_cli(["ctl", "init"], cwd=git_repo) == 0
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
        model=opencode_model,
        task_timeout=_LIVE_TASK_TIMEOUT_SECONDS,
    )

    assert completed == 1
    assert (git_repo / "live-proof.txt").read_text(encoding="utf-8") == "live test ok\n"
    assert (git_repo / ".jri" / "tasks" / "done" / "live-proof.md").exists()
    tags = git(git_repo, "tag").splitlines()
    assert "jri/begin/live-proof" in tags
    assert "jri/end/live-proof" in tags
