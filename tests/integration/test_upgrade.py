from pathlib import Path

import jri.core.git as git_module
import jri.core.service as service_module
from tests.conftest import run_cli
from tests.helpers import git, seed_task_and_commit


def test_init_upgrade_untracks_agent_files_from_older_repos(
    initialized_git_repo: Path,
    monkeypatch,
) -> None:
    git_repo = initialized_git_repo
    (git_repo / ".gitignore").write_text("dist/\n.opencode/\n", encoding="utf-8")
    (git_repo / ".opencode" / ".gitignore").write_text(
        "\n".join(
            [
                *service_module._MANAGED_AGENT_PATHS,
                *service_module._MANAGED_TOOL_PATHS,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    git(git_repo, "add", ".gitignore")
    git(git_repo, "add", "-f", ".opencode/.gitignore")
    git(git_repo, "commit", "-m", "add legacy ignore files")

    seed_task_and_commit(
        git_repo,
        status="todo",
        slug="keep-me",
        title="Keep me",
        priority=1,
        assignee="Human",
        body="Persistent task body.",
    )
    prompt_paths = {
        name: git_repo / ".opencode" / "agents" / name
        for name in service_module._MANAGED_AGENT_FILENAMES
    }
    tool_paths = {
        name: git_repo / ".opencode" / "tools" / name
        for name in service_module._MANAGED_TOOL_FILENAMES
    }
    config_paths = {
        name: git_repo / name for name in service_module._MANAGED_CONFIG_FILENAMES
    }

    def fake_load_prompt(name: str) -> str:
        base = name.removesuffix(".md") if name.endswith(".md") else name
        return f"upgraded {base}\n"

    monkeypatch.setattr(service_module, "_load_prompt", fake_load_prompt)
    (git_repo / "README.md").write_text("# changed\n", encoding="utf-8")
    (git_repo / "notes.txt").write_text("keep staged\n", encoding="utf-8")
    git(git_repo, "add", "notes.txt")

    exit_code = run_cli(["init", "--upgrade"], cwd=git_repo)

    assert exit_code == 0
    for name, path in prompt_paths.items():
        assert path.read_text(encoding="utf-8") == fake_load_prompt(name)
    for name, path in tool_paths.items():
        assert path.read_text(encoding="utf-8") == fake_load_prompt(name)
    for name, path in config_paths.items():
        assert path.read_text(encoding="utf-8") == fake_load_prompt(name)
    assert (git_repo / ".jri" / "tasks" / "todo" / "keep-me.md").exists()
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
    expected_changed = {
        ".gitignore",
        ".opencode/.gitignore",
        *service_module._MANAGED_AGENT_PATHS,
        *service_module._MANAGED_TOOL_PATHS,
        *service_module._MANAGED_CONFIG_FILENAMES,
    }
    assert changed_files == expected_changed
    status_lines = git(git_repo, "status", "--short").splitlines()
    for path in (*service_module._MANAGED_AGENT_PATHS, *service_module._MANAGED_TOOL_PATHS):
        assert f" M {path}" not in status_lines
        assert f"?? {path}" not in status_lines
    assert "README.md" in git(git_repo, "diff", "--name-only").splitlines()
    assert "notes.txt" in git(git_repo, "diff", "--cached", "--name-only").splitlines()


def test_init_upgrade_commits_when_config_files_change(
    initialized_git_repo: Path,
    monkeypatch,
) -> None:
    git_repo = initialized_git_repo

    def fake_load_prompt(name: str) -> str:
        base = name.removesuffix(".md") if name.endswith(".md") else name
        return f"upgraded {base}\n"

    monkeypatch.setattr(service_module, "_load_prompt", fake_load_prompt)

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
    assert changed_files == {
        *service_module._MANAGED_AGENT_PATHS,
        *service_module._MANAGED_TOOL_PATHS,
        *service_module._MANAGED_CONFIG_FILENAMES,
    }


def test_init_upgrade_cleans_legacy_gitignore_without_other_changes(
    initialized_git_repo: Path,
) -> None:
    git_repo = initialized_git_repo
    (git_repo / ".gitignore").write_text(".opencode/\n", encoding="utf-8")
    (git_repo / ".opencode" / ".gitignore").write_text(
        "\n".join(
            [
                *service_module._MANAGED_AGENT_PATHS,
                *service_module._MANAGED_TOOL_PATHS,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    git(git_repo, "add", ".gitignore")
    git(git_repo, "add", "-f", ".opencode/.gitignore")
    git(git_repo, "commit", "-m", "add legacy ignore files")

    exit_code = run_cli(["init", "--upgrade"], cwd=git_repo)

    assert exit_code == 0
    assert git(git_repo, "log", "-1", "--pretty=%s") == git_module.MSG_UPGRADE
    assert (git_repo / ".gitignore").read_text(encoding="utf-8").splitlines() == []
    assert not (git_repo / ".opencode" / ".gitignore").exists()
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
        ".gitignore",
        ".opencode/.gitignore",
    }
