import subprocess
from pathlib import Path

import pytest

import jri.core.git as git_module
from jri.cli.main import _build_parser
from tests.conftest import run_cli
from tests.helpers import git


def test_init_creates_scaffold_and_commit(
    git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = run_cli(["init"], cwd=git_repo)

    assert exit_code == 0
    assert "init: initialization complete." in capsys.readouterr().out
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "# temp repo\n"
    assert (git_repo / ".jri" / "learnings.md").read_text(encoding="utf-8") == ""
    assert (git_repo / ".jri" / "tasks" / "draft" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "tasks" / "todo" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "tasks" / "doing" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "tasks" / "done" / ".gitkeep").exists()
    assert (git_repo / ".jri" / "attempts" / ".gitkeep").exists()
    assert (git_repo / "Makefile").read_text(encoding="utf-8") == (
        ".PHONY: check\n\n"
        "check:\n"
        '\t@echo "make check is not configured yet"\n'
        "\t@false\n"
    )
    assert not (git_repo / ".jri" / "signals").exists()
    assert not (git_repo / ".jri" / "logs").exists()
    assert not (git_repo / ".jri" / "worktree").exists()
    assert (git_repo / ".jri" / "state.json").exists()
    assert not (git_repo / ".opencode").exists()
    assert not (git_repo / "opencode.json").exists()
    assert not (git_repo / ".jri" / ".opencode").exists()
    assert not (git_repo / ".jri" / "opencode.json").exists()
    assert not (git_repo / ".gitignore").exists()
    assert git(git_repo, "log", "-1", "--pretty=%s") == git_module.MSG_INIT
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
    assert "Makefile" in changed_files
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

    check = subprocess.run(
        ["make", "check"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode != 0
    assert "make check is not configured yet" in check.stdout


def test_init_creates_git_repo_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("GIT_AUTHOR_NAME", "JRI Tests")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "jri-tests@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "JRI Tests")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "jri-tests@example.com")

    exit_code = run_cli(["init"], cwd=repo)

    assert exit_code == 0
    assert "init: initialization complete." in capsys.readouterr().out
    assert (repo / ".git").exists()
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "log", "-1", "--pretty=%s") == git_module.MSG_INIT
    assert git(repo, "status", "--short") == ""


def test_init_creates_git_repo_on_requested_default_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("GIT_AUTHOR_NAME", "JRI Tests")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "jri-tests@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "JRI Tests")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "jri-tests@example.com")

    exit_code = run_cli(["init", "--branch", "trunk"], cwd=repo)

    assert exit_code == 0
    assert git(repo, "branch", "--show-current") == "trunk"
    assert git(repo, "log", "-1", "--pretty=%s") == git_module.MSG_INIT


def test_init_uses_requested_existing_default_branch(git_repo: Path) -> None:
    git(git_repo, "checkout", "-b", "trunk")
    git(git_repo, "checkout", "main")

    exit_code = run_cli(["init", "--branch", "trunk"], cwd=git_repo)

    assert exit_code == 0
    assert git(git_repo, "branch", "--show-current") == "trunk"
    assert git(git_repo, "log", "-1", "--pretty=%s") == git_module.MSG_INIT
    assert '"branch": "trunk"' in (git_repo / ".jri" / "state.json").read_text(
        encoding="utf-8"
    )


def test_init_creates_requested_branch_when_missing(git_repo: Path) -> None:
    main_ref = git(git_repo, "rev-parse", "main")

    exit_code = run_cli(["init", "--branch", "trunk"], cwd=git_repo)

    assert exit_code == 0
    assert git(git_repo, "branch", "--show-current") == "trunk"
    assert git(git_repo, "rev-parse", "trunk^") == main_ref
    assert git(git_repo, "rev-parse", "main") == main_ref
    assert '"branch": "trunk"' in (git_repo / ".jri" / "state.json").read_text(
        encoding="utf-8"
    )


def test_init_accepts_branch_short_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("GIT_AUTHOR_NAME", "JRI Tests")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "jri-tests@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "JRI Tests")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "jri-tests@example.com")

    exit_code = run_cli(["init", "-b", "trunk"], cwd=repo)

    assert exit_code == 0
    assert git(repo, "branch", "--show-current") == "trunk"


def test_init_help_includes_branch_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = _build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["init", "--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out

    assert "-b BRANCH" in help_text
    assert "--branch BRANCH" in help_text
    assert "--default-branch" not in help_text


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
    assert "Makefile" in changed_files
    assert ".jri/.gitignore" in changed_files
    assert ".gitignore" not in changed_files
    assert "README.md" in git(git_repo, "diff", "--name-only").splitlines()
    assert "notes.txt" in git(git_repo, "diff", "--cached", "--name-only").splitlines()


def test_init_creates_empty_readme_when_missing(git_repo: Path) -> None:
    (git_repo / "README.md").unlink()
    git(git_repo, "add", "README.md")
    git(git_repo, "commit", "-m", "remove readme")

    exit_code = run_cli(["init"], cwd=git_repo)

    assert exit_code == 0
    assert (git_repo / "README.md").read_text(encoding="utf-8") == ""
    assert (git_repo / ".jri" / "learnings.md").read_text(encoding="utf-8") == ""
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
    assert ".jri/learnings.md" in changed_files


def test_init_prompts_on_existing_dirs_and_aborts_when_no_input(git_repo: Path) -> None:
    """When run non-interactively (no stdin), init should abort when dirs exist."""
    assert run_cli(["init"], cwd=git_repo) == 0

    # Second init without force should abort when there's no input
    exit_code = run_cli(["init"], cwd=git_repo)

    # In non-interactive mode (EOFError), it should abort with exit code 1
    assert exit_code == 1


def test_init_delete_recreates_structure(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    extra = git_repo / ".jri" / "tasks" / "todo" / "extra.md"
    extra.write_text("temporary", encoding="utf-8")

    exit_code = run_cli(["init", "--delete"], cwd=git_repo)

    assert exit_code == 0
    assert not extra.exists()


def test_init_force_removes_custom_jri_state(git_repo: Path) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0

    custom_file = git_repo / ".jri" / "custom.txt"
    custom_file.parent.mkdir(parents=True, exist_ok=True)
    custom_file.write_text("custom content", encoding="utf-8")

    exit_code = run_cli(["init", "--force"], cwd=git_repo)

    assert exit_code == 0
    assert not custom_file.exists()
    assert not (git_repo / ".jri" / ".opencode").exists()


def test_init_leaves_existing_makefile_untouched(git_repo: Path) -> None:
    (git_repo / "Makefile").write_text("check:\n\t@echo custom\n", encoding="utf-8")

    exit_code = run_cli(["init"], cwd=git_repo)

    assert exit_code == 0
    assert (git_repo / "Makefile").read_text(encoding="utf-8") == (
        "check:\n\t@echo custom\n"
    )


@pytest.mark.parametrize(
    "args",
    [["upgrade"], ["init", "--upgrade"], ["init", "--bogus"]],
)
def test_init_rejects_removed_upgrade_command_and_unknown_flags(
    git_repo: Path,
    args: list[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run_cli(args, cwd=git_repo)

    assert exc_info.value.code == 2
