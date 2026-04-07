from pathlib import Path

import jri.core.git as git_module
import jri.core.service as service_module
from tests.conftest import run_cli
from tests.helpers import git


def test_init_creates_scaffold_and_commit(git_repo: Path) -> None:
    exit_code = run_cli(["init"], cwd=git_repo)

    assert exit_code == 0
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "# temp repo\n"
    assert (git_repo / ".jri" / "tasks" / "draft" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "tasks" / "todo" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "tasks" / "doing" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "tasks" / "done" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "signals").is_dir()
    assert (git_repo / ".jri" / "logs" / "external").is_dir()
    assert (git_repo / ".jri" / "state.json").exists()
    for name in service_module._MANAGED_AGENT_FILENAMES:
        assert (git_repo / ".opencode" / "agents" / name).exists()
    for name in service_module._MANAGED_CONFIG_FILENAMES:
        assert (git_repo / name).exists()
    assert git(git_repo, "log", "-1", "--pretty=%s") == git_module.MSG_INIT
    assert (git_repo / ".gitignore").read_text(encoding="utf-8").splitlines() == [
        *service_module._MANAGED_AGENT_PATHS,
        *service_module._MANAGED_TOOL_PATHS,
    ]
    assert (git_repo / ".jri" / ".gitignore").read_text(
        encoding="utf-8"
    ).splitlines() == [
        "logs/",
        "signals/",
        "*state.json*",
        "metrics.json",
        "worktree/",
    ]
    assert git(git_repo, "status", "--short") == ""


def test_init_commits_only_scaffold_when_unrelated_changes_exist(
    git_repo: Path,
) -> None:
    (git_repo / "README.md").write_text("# changed\n", encoding="utf-8")
    (git_repo / "notes.txt").write_text("keep staged\n", encoding="utf-8")
    git(git_repo, "add", "notes.txt")

    exit_code = run_cli(["init"], cwd=git_repo)

    assert exit_code == 0
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
    assert "README.md" not in changed_files
    assert "notes.txt" not in changed_files
    assert ".jri/.gitignore" in changed_files
    assert ".gitignore" in changed_files
    assert "README.md" in git(git_repo, "diff", "--name-only").splitlines()
    assert "notes.txt" in git(git_repo, "diff", "--cached", "--name-only").splitlines()


def test_init_appends_agent_ignore_rule_to_existing_gitignore(git_repo: Path) -> None:
    (git_repo / ".gitignore").write_text("dist/\n", encoding="utf-8")

    exit_code = run_cli(["init"], cwd=git_repo)

    assert exit_code == 0
    assert (git_repo / ".gitignore").read_text(encoding="utf-8").splitlines() == [
        "dist/",
        "",
        *service_module._MANAGED_AGENT_PATHS,
        *service_module._MANAGED_TOOL_PATHS,
    ]


def test_init_creates_empty_readme_when_missing(git_repo: Path) -> None:
    (git_repo / "README.md").unlink()
    git(git_repo, "add", "README.md")
    git(git_repo, "commit", "-m", "remove readme")

    exit_code = run_cli(["init"], cwd=git_repo)

    assert exit_code == 0
    assert (git_repo / "README.md").read_text(encoding="utf-8") == ""
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
    assert "README.md" in changed_files


def test_init_prompts_on_existing_dirs_and_aborts_when_no_input(git_repo: Path) -> None:
    """When run non-interactively (no stdin), init should abort when dirs exist."""
    assert run_cli(["init"], cwd=git_repo) == 0

    # Second init without force should abort when there's no input
    exit_code = run_cli(["init"], cwd=git_repo)

    # In non-interactive mode (EOFError), it should abort with exit code 1
    assert exit_code == 1


def test_init_force_recreates_structure(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    extra = git_repo / ".jri" / "tasks" / "todo" / "extra.md"
    extra.write_text("temporary", encoding="utf-8")

    exit_code = run_cli(["init", "--force"], cwd=git_repo)

    assert exit_code == 0
    assert not extra.exists()


def test_init_force_removes_opencode_dir(git_repo: Path) -> None:
    """Force flag should also remove .opencode/ directory."""
    assert run_cli(["init"], cwd=git_repo) == 0

    # Add a custom file to .opencode/
    custom_file = git_repo / ".opencode" / "custom.txt"
    custom_file.write_text("custom content", encoding="utf-8")

    # Force reinit should remove .opencode/ entirely
    exit_code = run_cli(["init", "--force"], cwd=git_repo)

    assert exit_code == 0
    assert not custom_file.exists()
    # Managed files should be recreated
    assert (git_repo / ".opencode" / "agents" / "interrogator.md").exists()
