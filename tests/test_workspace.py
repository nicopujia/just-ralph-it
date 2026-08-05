from pathlib import Path

import pytest
import yaml

from jri.core import paths
from jri.core.conversation import Conversation
from jri.core.settings import Settings
from jri.core.workspace import Installation, Workspace
from jri.lib import git
from tests.conftest import CreateRepository, RunGit
from tests.doubles.openai import FakeClient
from tests.doubles.settings import build_settings
from tests.doubles.workspace import install_workspace


def test_initializes_a_workspace_ready_to_use(tmp_path: Path) -> None:
    installation = install_workspace(tmp_path)

    assert installation == Installation(Workspace(tmp_path), created=True, repository_created=True)
    assert installation.workspace.directory == tmp_path / paths.WORKSPACE_DIR
    assert installation.workspace.config_file == tmp_path / paths.CONFIG_FILE
    assert (tmp_path / paths.CONFIG_FILE).read_text() == Settings.render_config()
    assert (tmp_path / paths.GITIGNORE_FILE).read_text() == "session.json\nlogs\nvisualization.html\n"
    assert yaml.safe_load((tmp_path / paths.NOTEBOOK_FILE).read_text()) == {
        "topics": [{"id": "t1", "name": "Project overview", "status": "open", "notes": {}}],
        "connections": [],
        "next_note_id": "n1",
    }
    assert list((tmp_path / paths.LOGS_DIR).iterdir()) == []


def test_leaves_the_project_uncommitted_when_it_creates_the_repository(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / ".DS_Store").write_bytes(b"\x00")

    install_workspace(tmp_path)

    repository = git.Repository(tmp_path)
    assert not repository.has_commit()
    assert (tmp_path / paths.PROJECT_GITIGNORE_FILE).read_text() == ".DS_Store\n.env\n.env.*\n"
    # The secrets are ignored rather than untracked, so the first
    # commit the user makes of their own project leaves them out.
    assert {item.path for item in repository.read_status()} == {
        paths.PROJECT_GITIGNORE_FILE,
        paths.GITIGNORE_FILE,
        paths.CONFIG_FILE,
        paths.NOTEBOOK_FILE,
        "main.py",
    }


def test_keeps_an_existing_ignore_file_when_creating_the_repository(tmp_path: Path) -> None:
    (tmp_path / paths.PROJECT_GITIGNORE_FILE).write_text("build/\n")

    install_workspace(tmp_path)

    assert (tmp_path / paths.PROJECT_GITIGNORE_FILE).read_text() == "build/\n"


def test_leaves_a_repository_without_commits_alone(tmp_path: Path, run_git: RunGit) -> None:
    run_git(tmp_path, "init", "-q")
    (tmp_path / "main.py").write_text("print('hello')\n")

    installation = install_workspace(tmp_path)

    assert not installation.repository_created
    assert not git.Repository(tmp_path).has_commit()
    assert not (tmp_path / paths.PROJECT_GITIGNORE_FILE).exists()


def test_finds_the_workspace_at_the_root_of_the_enclosing_repository(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = create_repository(tmp_path / "repo")
    nested = repository.path / "packages" / "app"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert Workspace.find().root == repository.path


def test_falls_back_to_the_working_directory_outside_a_repository(tmp_path: Path) -> None:
    assert Workspace.find().root == tmp_path


def test_initializes_a_workspace_inside_an_existing_repository(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")
    nested = repository.path / "packages" / "app"
    nested.mkdir(parents=True)

    installation = install_workspace(nested)

    assert not installation.repository_created
    assert repository.read_head() == git.Repository(nested).read_head()
    assert (nested / paths.CONFIG_FILE).exists()


def test_creates_the_working_directory_when_it_is_missing(tmp_path: Path) -> None:
    missing = tmp_path / "new" / "project"

    installation = install_workspace(missing)

    assert installation.repository_created
    assert installation.workspace.directory == missing / paths.WORKSPACE_DIR
    assert (missing / paths.CONFIG_FILE).exists()
    assert git.find_root(missing) == missing.resolve()


def test_preserves_an_existing_workspace_when_initializing_again(tmp_path: Path) -> None:
    (tmp_path / paths.WORKSPACE_DIR).mkdir()
    (tmp_path / paths.CONFIG_FILE).write_text("custom config\n")
    (tmp_path / paths.GITIGNORE_FILE).write_text("custom-cache\nlogs")

    install_workspace(tmp_path)
    installation = install_workspace(tmp_path)

    assert not installation.created
    assert (tmp_path / paths.CONFIG_FILE).read_text() == "custom config\n"
    assert (tmp_path / paths.GITIGNORE_FILE).read_text() == "custom-cache\nlogs\nsession.json\nvisualization.html\n"


def test_starts_the_workspace_over_when_initialization_is_forced(tmp_path: Path) -> None:
    notebook = {
        "topics": [{"id": "t1", "name": "Project overview", "status": "open", "notes": {"n1": "Keep this note."}}],
        "connections": [],
        "next_note_id": "n2",
    }
    install_workspace(tmp_path)
    (tmp_path / paths.CONFIG_FILE).write_text("custom config\n")
    (tmp_path / paths.NOTEBOOK_FILE).write_text(yaml.safe_dump(notebook))

    installation = install_workspace(tmp_path, force=True)

    assert not installation.created
    assert (tmp_path / paths.CONFIG_FILE).read_text() == Settings.render_config()
    assert yaml.safe_load((tmp_path / paths.NOTEBOOK_FILE).read_text()) == {
        "topics": [{"id": "t1", "name": "Project overview", "status": "open", "notes": {}}],
        "connections": [],
        "next_note_id": "n1",
    }


def test_resets_an_invalid_workspace_when_forced(tmp_path: Path) -> None:
    base_dir = tmp_path / ".jri"
    base_dir.mkdir()
    config = base_dir / "config.yaml"
    config.write_text("custom config")
    (base_dir / ".gitignore").write_text("custom-cache\n")
    (base_dir / "notebook.yaml").write_text(": invalid yaml")
    (base_dir / "session.json").write_text("not json")
    (base_dir / "visualization.html").write_text("old graph")
    (base_dir / "logs").mkdir()
    (base_dir / "logs" / "old.log").write_text("old log")
    (base_dir / "specs").mkdir()
    (base_dir / "specs" / "old.md").write_text("old spec")

    install_workspace(tmp_path, force=True)
    conversation = Conversation(build_settings(FakeClient([])))

    assert conversation.restore() == []
    assert conversation.session.show_thinking_blocks is False
    assert [(topic.id, topic.name) for topic in conversation.interviewer.notebook.graph.topics] == [
        ("t1", "Project overview")
    ]
    assert conversation.workspace.notebook_file == base_dir / "notebook.yaml"
    assert config.read_text() == Settings.render_config()
    assert not conversation.workspace.session_file.exists()
    assert not (base_dir / "visualization.html").exists()
    assert not (base_dir / "specs").exists()
    assert not (base_dir / "logs" / "old.log").exists()
    assert (base_dir / ".gitignore").read_text() == "custom-cache\nsession.json\nlogs\nvisualization.html\n"


def test_keeps_the_rest_of_the_project_when_resetting_the_workspace(tmp_path: Path) -> None:
    for name in (paths.ARCHITECTURE_SPECS_ROOT, paths.FUNCTIONAL_SPECS_ROOT, "src"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "file.md").write_text("project content")

    install_workspace(tmp_path, force=True)

    kept = {".jri", ".git", paths.PROJECT_GITIGNORE_FILE}
    assert [
        (path.name, (path / "file.md").read_text()) for path in sorted(tmp_path.iterdir()) if path.name not in kept
    ] == [("architecture", "project content"), ("functional", "project content"), ("src", "project content")]
