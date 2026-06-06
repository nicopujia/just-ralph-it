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
