from pathlib import Path

from tests.conftest import run_cli
from tests.helpers import git


def test_init_creates_scaffold_and_commit(git_repo: Path) -> None:
    exit_code = run_cli(["init"], cwd=git_repo)

    assert exit_code == 0
    assert (git_repo / ".jri" / "tasks" / "draft" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "tasks" / "todo" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "tasks" / "doing" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "tasks" / "done" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "signals").is_dir()
    assert (git_repo / ".jri" / "logs" / "external").is_dir()
    assert (git_repo / ".jri" / "state.json").exists()
    assert (git_repo / ".opencode" / "agents" / "interrogator.md").exists()
    assert (git_repo / ".opencode" / "agents" / "ralph.md").exists()
    assert git(git_repo, "log", "-1", "--pretty=%s") == "jri init"
    assert (git_repo / ".jri" / ".gitignore").read_text(
        encoding="utf-8"
    ).splitlines() == [
        "logs/",
        "signals/",
        "state.json",
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
    assert ".opencode/agents/interrogator.md" in changed_files
    assert "README.md" in git(git_repo, "diff", "--name-only").splitlines()
    assert "notes.txt" in git(git_repo, "diff", "--cached", "--name-only").splitlines()


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
