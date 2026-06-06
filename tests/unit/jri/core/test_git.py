"""Tests for JRI-owned Git operations."""

import subprocess
from pathlib import Path

import pytest

from jri.core.git import GitError, commit_jri_files


def test_commit_jri_files_succeeds_without_committable_paths(
    tmp_path: Path,
) -> None:
    """No JRI docs means there is nothing to commit."""
    project = _make_repo(tmp_path)

    result = commit_jri_files(project)

    assert not result.committed
    assert result.message == "No .jri changes to commit."


def test_commit_jri_files_raises_git_error_outside_repo(
    tmp_path: Path,
) -> None:
    """Git failures are surfaced as GitError."""
    (tmp_path / ".jri" / "specs").mkdir(parents=True)
    (tmp_path / ".jri" / "specs" / "product.md").write_text("# Product\n")

    with pytest.raises(GitError):
        commit_jri_files(tmp_path)


def test_commit_jri_files_returns_stable_message_without_git_summary(
    tmp_path: Path,
) -> None:
    """Successful commits report app-level status, not raw git summary."""
    project = _make_repo(tmp_path)
    (project / ".jri" / "specs").mkdir(parents=True)
    (project / ".jri" / ".gitignore").write_text("logs/\n", encoding="utf-8")
    (project / ".jri" / "scratchpad.md").write_text(
        "# Scratchpad\n",
        encoding="utf-8",
    )
    (project / ".jri" / "specs" / "product.md").write_text(
        "# Product\n",
        encoding="utf-8",
    )

    result = commit_jri_files(project)

    assert result.committed
    assert result.message == "Committed .jri changes."
    assert "create mode" not in result.message


def test_commit_jri_files_commits_for_fresh_user_without_git_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh users can finalize specs without global Git identity config."""
    fresh_home = tmp_path / "fresh-home"
    fresh_home.mkdir()
    monkeypatch.setenv("HOME", str(fresh_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fresh_home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in [
        "EMAIL",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_NAME",
    ]:
        monkeypatch.delenv(name, raising=False)
    project = _make_repo_without_identity(tmp_path)
    (project / ".jri" / "specs").mkdir(parents=True)
    (project / ".jri" / ".gitignore").write_text("logs/\n", encoding="utf-8")
    (project / ".jri" / "scratchpad.md").write_text(
        "# Scratchpad\n",
        encoding="utf-8",
    )
    (project / ".jri" / "specs" / "product.md").write_text(
        "# Product\n",
        encoding="utf-8",
    )

    result = commit_jri_files(project)

    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert result.committed
    assert committed == [
        ".jri/.gitignore",
        ".jri/scratchpad.md",
        ".jri/specs/product.md",
    ]
    assert not (fresh_home / ".gitconfig").exists()
    assert not (fresh_home / ".config" / "git" / "config").exists()


def test_commit_jri_files_includes_tracked_deleted_specs(
    tmp_path: Path,
) -> None:
    """Tracked deleted JRI specs are included in the handoff commit."""
    project = _make_repo(tmp_path)
    specs = project / ".jri" / "specs"
    specs.mkdir(parents=True)
    product = specs / "product.md"
    product.write_text("# Product\n", encoding="utf-8")
    subprocess.run(["git", "add", ".jri"], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "-m", "docs: capture JRI specs"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    product.unlink()

    result = commit_jri_files(project)

    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert result.committed
    assert committed == [".jri/specs/product.md"]
    assert not status


def _make_repo(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(
        ["git", "init"], cwd=project, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "jri@example.com"],
        cwd=project,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "JRI Tests"],
        cwd=project,
        check=True,
    )
    return project


def _make_repo_without_identity(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(
        ["git", "init"], cwd=project, check=True, capture_output=True
    )
    return project
