from pathlib import Path

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
    assert git(git_repo, "log", "-1", "--pretty=%s") == "jri init"
    assert (git_repo / ".gitignore").read_text(encoding="utf-8").splitlines() == list(
        service_module._MANAGED_AGENT_PATHS
    )
    assert (git_repo / ".jri" / ".gitignore").read_text(
        encoding="utf-8"
    ).splitlines() == [
        "logs/",
        "signals/",
        "state.json",
        "state.json.bak",
        ".state.json.tmp",
        ".state.json.bak.tmp",
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


def test_init_refuses_to_overwrite_without_force(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    exit_code = run_cli(["init"], cwd=git_repo)

    assert exit_code == 1


def test_init_force_recreates_structure(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    extra = git_repo / ".jri" / "tasks" / "todo" / "extra.md"
    extra.write_text("temporary", encoding="utf-8")

    exit_code = run_cli(["init", "--force"], cwd=git_repo)

    assert exit_code == 0
    assert not extra.exists()
