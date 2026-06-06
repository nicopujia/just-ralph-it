"""Tests for project state discovery and initialization."""

import subprocess
from pathlib import Path

from jri.core.project import find_project_root, initialize_project


def test_project_root_uses_parent_jri_when_present(tmp_path: Path) -> None:
    """An existing parent .jri directory owns child sessions."""
    project = tmp_path / "project"
    child = project / "app" / "api"
    child.mkdir(parents=True)
    (project / ".jri").mkdir()

    assert find_project_root(child) == project


def test_project_root_accepts_file_start_paths(tmp_path: Path) -> None:
    """File paths resolve through their parent directory."""
    project = tmp_path / "project"
    source = project / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("print('hi')\n")
    (project / ".jri").mkdir()

    assert find_project_root(source) == project


def test_project_root_uses_git_root_when_no_jri_exists(
    tmp_path: Path,
) -> None:
    """A Git worktree owns sessions when no .jri exists."""
    project = tmp_path / "project"
    child = project / "app" / "api"
    child.mkdir(parents=True)
    subprocess.run(
        ["git", "init"], cwd=project, check=True, capture_output=True
    )

    assert find_project_root(child) == project


def test_project_root_uses_current_directory_without_jri_or_git(
    tmp_path: Path,
) -> None:
    """A plain directory owns its own new JRI project."""
    project = tmp_path / "new-project"
    project.mkdir()

    assert find_project_root(project) == project


def test_initialization_creates_git_and_jri_structure(
    tmp_path: Path,
) -> None:
    """Initialization prepares a plain directory for one JRI session."""
    project = tmp_path / "new-project"
    project.mkdir()

    state = initialize_project(project)

    assert state.root == project
    assert (project / ".git").is_dir()
    assert (project / ".jri" / ".gitignore").read_text() == "logs/\n"
    assert (project / ".jri" / "scratchpad.md").read_text() == (
        "# Scratchpad\n\n## Open Topics\n\n## Pending Questions\n\n## Notes\n"
    )
    assert (project / ".jri" / "specs").is_dir()
    assert (project / ".jri" / "logs").is_dir()
    assert not (project / ".jri" / "logs" / "interview.jsonl").read_text()


def test_initialization_preserves_existing_jri_state(tmp_path: Path) -> None:
    """Initialization fills gaps without overwriting content."""
    project = tmp_path / "project"
    specs = project / ".jri" / "specs"
    log = project / ".jri" / "logs" / "interview.jsonl"
    specs.mkdir(parents=True)
    log.parent.mkdir(parents=True)
    (project / ".jri" / ".gitignore").write_text("custom\n")
    (project / ".jri" / "scratchpad.md").write_text("existing notes\n")
    (specs / "product.md").write_text("existing spec\n")
    log.write_text('{"type": "existing"}\n')

    initialize_project(project)

    assert (project / ".jri" / ".gitignore").read_text() == "custom\n"
    assert (project / ".jri" / "scratchpad.md").read_text() == (
        "existing notes\n"
    )
    assert (specs / "product.md").read_text() == "existing spec\n"
    assert log.read_text() == '{"type": "existing"}\n'


def test_force_recreates_only_resolved_jri_directory(
    tmp_path: Path,
) -> None:
    """Force reset deletes only the active JRI state directory."""
    project = tmp_path / "project"
    child = project / "app" / "api"
    specs = project / ".jri" / "specs"
    child.mkdir(parents=True)
    specs.mkdir(parents=True)
    subprocess.run(
        ["git", "init"], cwd=project, check=True, capture_output=True
    )
    (project / ".jri" / "scratchpad.md").write_text("delete me\n")
    (specs / "product.md").write_text("delete me\n")
    (project / "source.py").write_text("keep me\n")

    state = initialize_project(child, force=True)

    assert state.root == project
    assert (project / ".git").is_dir()
    assert (project / "source.py").read_text() == "keep me\n"
    assert not (specs / "product.md").exists()
    assert (project / ".jri" / "scratchpad.md").read_text() == (
        "# Scratchpad\n\n## Open Topics\n\n## Pending Questions\n\n## Notes\n"
    )
