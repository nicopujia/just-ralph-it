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
