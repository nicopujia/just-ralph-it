"""Tests for JRI finalization."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from jri.core.tools.just_ralph_it import (
    JustRalphItError,
    finalize_jri,
)


def test_just_ralph_it_fails_without_trigger(tmp_path: Path) -> None:
    """Finalization requires the latest user message to be a trigger."""
    with pytest.raises(JustRalphItError, match="trigger"):
        asyncio.run(
            finalize_jri(
                project_root=tmp_path,
                latest_user_message="not yet",
                readiness_summary="Ready.",
            )
        )


def test_just_ralph_it_fails_with_known_blockers(tmp_path: Path) -> None:
    """Finalization refuses to commit when blockers remain."""
    with pytest.raises(JustRalphItError, match="Missing target user"):
        asyncio.run(
            finalize_jri(
                project_root=tmp_path,
                latest_user_message="just ralph it",
                readiness_summary="Not ready.",
                known_blockers=["Missing target user"],
            )
        )


def test_just_ralph_it_fails_without_persisted_specs(
    tmp_path: Path,
) -> None:
    """Finalization requires at least one persisted spec file."""
    project = _make_repo(tmp_path)
    (project / ".jri" / "specs").mkdir(parents=True)
    (project / ".jri" / ".gitignore").write_text("logs/\n")
    (project / ".jri" / "scratchpad.md").write_text("# Scratchpad\n")

    with pytest.raises(JustRalphItError, match="spec"):
        asyncio.run(
            finalize_jri(
                project_root=project,
                latest_user_message="just ralph it",
                readiness_summary="Ready.",
            )
        )


def test_just_ralph_it_commits_only_committable_jri_files(
    tmp_path: Path,
) -> None:
    """Finalization commits JRI docs, not logs or project files."""
    project = _make_repo(tmp_path)
    (project / ".jri" / "specs").mkdir(parents=True)
    (project / ".jri" / "logs").mkdir()
    (project / ".jri" / ".gitignore").write_text("logs/\n")
    (project / ".jri" / "scratchpad.md").write_text("# Scratchpad\n")
    (project / ".jri" / "specs" / "product.md").write_text("# Product\n")
    (project / ".jri" / "logs" / "interview.jsonl").write_text(
        '{"type": "user_message"}\n'
    )
    (project / "README.md").write_text("# Project\n")

    result = asyncio.run(
        finalize_jri(
            project_root=project,
            latest_user_message="just ralph it",
            readiness_summary="Ready.",
        )
    )

    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert result.should_exit
    assert "Ralph is coming soon to JRI" in result.message
    assert (
        "For now, you need to figure out how to implement the specs yourself"
        in result.message
    )
    assert "handoff" not in result.message.lower()
    assert "built" not in result.message.lower()
    assert set(committed) == {
        ".jri/.gitignore",
        ".jri/scratchpad.md",
        ".jri/specs/product.md",
    }
    assert (project / ".jri" / "logs" / "interview.jsonl").exists()


def test_just_ralph_it_exits_successfully_without_empty_commit(
    tmp_path: Path,
) -> None:
    """Finalization succeeds without a commit when unchanged."""
    project = _make_repo(tmp_path)
    (project / ".jri" / "specs").mkdir(parents=True)
    (project / ".jri" / ".gitignore").write_text("logs/\n")
    (project / ".jri" / "scratchpad.md").write_text("# Scratchpad\n")
    (project / ".jri" / "specs" / "product.md").write_text("# Product\n")
    subprocess.run(["git", "add", ".jri"], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "-m", "docs: capture JRI specs"],
        cwd=project,
        check=True,
        capture_output=True,
    )

    result = asyncio.run(
        finalize_jri(
            project_root=project,
            latest_user_message="just ralph it",
            readiness_summary="Ready.",
        )
    )

    commits = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert result.should_exit
    assert commits == "1"


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
