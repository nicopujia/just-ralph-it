from pathlib import Path

import jri.core.git as git_module
import jri.core.service as service_module
from tests.conftest import run_cli
from tests.helpers import git


def test_init_upgrade_commits_when_runtime_gitignore_changes(
    git_repo: Path,
    monkeypatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    monkeypatch.setattr(
        service_module.JriService,
        "_GITIGNORE_CONTENT",
        "logs/\nsignals/\n*state.json*\nmetrics.json\nworktree/\ncache/\n",
    )

    exit_code = run_cli(["init", "--upgrade"], cwd=git_repo)

    assert exit_code == 0
    assert git(git_repo, "log", "-1", "--pretty=%s") == git_module.MSG_UPGRADE
    changed_files = set(
        git(
            git_repo,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "HEAD",
        ).splitlines()
    )
    assert changed_files == {".jri/.gitignore"}
