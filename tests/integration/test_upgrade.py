from pathlib import Path

import jri.core.service as service_module
from tests.conftest import run_cli
from tests.helpers import git, write_task


def test_upgrade_refreshes_managed_files_and_commits_only_them(
    git_repo: Path,
    monkeypatch,
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    write_task(
        git_repo,
        status="todo",
        slug="keep-me",
        title="Keep me",
        priority=1,
        assignee="Human",
        body="Persistent task body.",
    )
    prompt_path = git_repo / ".opencode" / "agents" / "interrogator.md"
    ralph_prompt_path = git_repo / ".opencode" / "agents" / "ralph.md"

    def fake_load_prompt(name: str) -> str:
        return {
            "interrogator.md": "upgraded interrogator\n",
            "ralph.md": "upgraded ralph\n",
        }[name]

    monkeypatch.setattr(service_module, "_load_prompt", fake_load_prompt)
    (git_repo / "README.md").write_text("# changed\n", encoding="utf-8")
    (git_repo / "notes.txt").write_text("keep staged\n", encoding="utf-8")
    git(git_repo, "add", "notes.txt")

    exit_code = run_cli(["upgrade"], cwd=git_repo)

    assert exit_code == 0
    assert prompt_path.read_text(encoding="utf-8") == "upgraded interrogator\n"
    assert ralph_prompt_path.read_text(encoding="utf-8") == "upgraded ralph\n"
    assert (git_repo / ".jri" / "tasks" / "todo" / "keep-me.md").exists()
    assert git(git_repo, "log", "-1", "--pretty=%s") == "jri upgrade"

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
    assert changed_files == {
        ".opencode/agents/interrogator.md",
        ".opencode/agents/ralph.md",
    }
    assert "README.md" in git(git_repo, "diff", "--name-only").splitlines()
    assert "notes.txt" in git(git_repo, "diff", "--cached", "--name-only").splitlines()
