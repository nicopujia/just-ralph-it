import os
from pathlib import Path

import pytest

from jri.core.opencode import OpenCodeClient
from jri.core.service import JriService
from tests.conftest import run_cli
from tests.helpers import git, write_task

pytestmark = pytest.mark.live


def test_start_with_real_opencode_completes_trivial_task(git_repo: Path) -> None:
    if os.getenv("JRI_RUN_LIVE_OPENCODE") != "1":
        pytest.skip("set JRI_RUN_LIVE_OPENCODE=1 to enable live OpenCode tests")

    assert run_cli(["init"], cwd=git_repo) == 0
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

    service = JriService(
        git_repo,
        opencode_client=OpenCodeClient(
            model=os.getenv("JRI_TEST_MODEL", "opencode/qwen3.6-plus-free")
        ),
    )

    completed = service.start(iterations=1)

    assert completed == 1
    assert (git_repo / "live-proof.txt").read_text(encoding="utf-8") == "live test ok\n"
    assert (git_repo / ".jri" / "tasks" / "done" / "live-proof.md").exists()
    assert "jri/1" in git(git_repo, "tag").splitlines()
