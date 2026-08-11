import os
import sys
import time
from pathlib import Path

import pytest
import yaml

from jri.core import paths
from jri.core.conversation import Conversation
from jri.core.exceptions import PersistenceError
from jri.core.settings import Settings
from jri.core.workspace import MAX_PID, Hold, Installation, Workspace
from jri.lib import git
from tests.conftest import CreateRepository, RunGit
from tests.doubles.acceptance import ROOT_QUESTION, WINDOW_MARKER, install_a_killing_git
from tests.doubles.lock import hold, take
from tests.doubles.openai import FakeClient
from tests.doubles.settings import build_settings
from tests.doubles.workspace import (
    hold_workspace,
    hold_workspace_slowly,
    install_workspace,
    run_a_bystander,
    watch_a_bystander,
)

# This test data supports the tests below.
# This test data supports the tests below.
RECORDS_AFTER = 0.4


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
    # Check that JRI leaves the project uncommitted when it creates the repository.
    # Check that JRI leaves the project uncommitted when it creates the repository.
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


@pytest.mark.skipif(sys.platform == "win32", reason="a Git that ends itself needs a shell and `kill`")
def test_refuses_a_root_a_killed_git_never_placed(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = create_repository(tmp_path / "repo")
    nested = repository.path / "packages" / "app"
    nested.mkdir(parents=True)
    (repository.path / ".git" / WINDOW_MARKER).touch()
    monkeypatch.chdir(nested)
    install_a_killing_git(monkeypatch, repository.path, ROOT_QUESTION)

    with pytest.raises(git.Error):
        Workspace.find()
    with pytest.raises(git.Error):
        install_workspace(nested)

    # Check that JRI refuses a root a killed git never placed.
    # Check that JRI refuses a root a killed git never placed.
    # Check that JRI refuses a root a killed git never placed.
    assert not (nested / paths.WORKSPACE_DIR).exists()
    assert not (nested / ".git").exists()


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
    # Check that JRI resets an invalid workspace when forced.
    # Check that JRI resets an invalid workspace when forced.
    # Check that JRI resets an invalid workspace when forced.
    assert (base_dir / ".gitignore").read_text() == (
        "custom-cache\n/lock\n/lock.claim\nsession.json\nlogs\nvisualization.html\n"
    )


def test_keeps_a_run_directory_out_of_the_project(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    create_repository(tmp_path)
    workspace = install_workspace(tmp_path).workspace
    repository = git.Repository(tmp_path)
    tracked = repository.read_worktree_paths()

    (workspace.open_generation_dir() / "journal.jsonl").write_text("what a model said\n")

    assert repository.read_worktree_paths() == tracked
    assert not repository.read_status((paths.GENERATION_DIR,))
    assert run_git(tmp_path, "check-ignore", "-v", f"{paths.GENERATION_DIR}/journal.jsonl").startswith(
        f"{paths.GITIGNORE_FILE}:"
    )
    run_git(tmp_path, "add", "-A")
    staged = repository.read_staged_paths()
    assert not [path for path in staged if path.startswith(paths.GENERATION_DIR)]
    # Check that JRI keeps a run directory out of the project.
    # Check that JRI keeps a run directory out of the project.
    # Check that JRI keeps a run directory out of the project.
    assert paths.GITIGNORE_FILE in staged


def test_puts_back_a_run_directory_rule_something_replaced(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    create_repository(tmp_path)
    workspace = install_workspace(tmp_path).workspace
    repository = git.Repository(tmp_path)
    (workspace.open_generation_dir() / "journal.jsonl").write_text("what a model said\n")
    # Check that JRI puts back a run directory rule something replaced.
    # Check that JRI puts back a run directory rule something replaced.
    # Check that JRI puts back a run directory rule something replaced.
    for rule in sorted(workspace.directory.rglob(workspace.gitignore_file.name)):
        rule.write_text("# nothing to ignore here\n")

    workspace.open_generation_dir()

    assert not repository.read_status((paths.GENERATION_DIR,))
    run_git(tmp_path, "add", "-A")
    assert not [path for path in repository.read_staged_paths() if path.startswith(paths.GENERATION_DIR)]


def test_clears_a_run_directory_a_reset_asks_for(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    (workspace.open_generation_dir() / "journal.jsonl").write_text("what a model said\n")

    install_workspace(tmp_path, force=True)

    assert not workspace.generation_dir.exists()


def test_refuses_a_reset_while_a_window_has_the_project(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    (workspace.open_generation_dir() / "journal.jsonl").write_text("what a model said\n")
    workspace.draft_file.write_text("diff --git a/x b/x\n")
    (tmp_path / paths.FUNCTIONAL_SPECS_DIR).mkdir(parents=True)
    (tmp_path / paths.FUNCTIONAL_SPECS_DIR / "behavior.md").write_text("# what the project does\n")

    with hold_workspace(tmp_path) as window:
        with pytest.raises(PersistenceError, match=str(window.pid)):
            install_workspace(tmp_path, force=True)

        assert window.poll() is None, "the window holding the project was left to write into a workspace that went"
        assert workspace.draft_file.read_text() == "diff --git a/x b/x\n"
        assert (workspace.generation_dir / "journal.jsonl").read_text() == "what a model said\n"
        assert (tmp_path / paths.FUNCTIONAL_SPECS_DIR / "behavior.md").read_text() == "# what the project does\n"
        assert workspace.notebook_file.exists()


def test_refuses_a_reset_while_a_run_is_still_going(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    journal = workspace.open_generation_dir() / "journal.jsonl"
    journal.write_text("what a model said\n")

    with hold(tmp_path / paths.GENERATION_LOCK_FILE) as runner:
        with pytest.raises(PersistenceError, match="run is still going"):
            install_workspace(tmp_path, force=True)

        assert runner.poll() is None, "the run was left writing into a directory that went"
        assert journal.read_text() == "what a model said\n"
        # Check that JRI refuses a reset while a run is still going.
        # Check that JRI refuses a reset while a run is still going.
        assert not take(tmp_path / paths.GENERATION_LOCK_FILE)


# This test data supports the tests below.
# This test data supports the tests below.
# This test data supports the tests below.
# This test data supports the tests below.
def test_refuses_a_held_project_before_naming_what_a_reset_replaces(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    named: list[tuple[Path, ...]] = []

    with hold_workspace(tmp_path) as window:
        with pytest.raises(PersistenceError, match=str(window.pid)), Workspace(tmp_path).open_reset() as reset:
            named.append(reset.paths)

        assert not named, "a refusal was read only after something had been asked about the deletion"


def test_refuses_a_running_run_before_naming_what_a_reset_replaces(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    workspace.open_generation_dir()
    named: list[tuple[Path, ...]] = []

    with hold(tmp_path / paths.GENERATION_LOCK_FILE):
        with pytest.raises(PersistenceError, match="run is still going"), workspace.open_reset() as reset:
            named.append(reset.paths)

        assert not named, "a refusal was read only after something had been asked about the deletion"


# This test data supports the tests below.
# This test data supports the tests below.
# This test data supports the tests below.
# This test data supports the tests below.
# This test data supports the tests below.
def test_keeps_the_project_while_a_reset_is_open(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace

    with workspace.open_reset():
        assert not take(tmp_path / paths.LOCK_FILE)

    assert take(tmp_path / paths.LOCK_FILE)


# This test data supports the tests below.
# This test data supports the tests below.
def test_names_what_a_reset_replaces_and_nothing_it_would_leave(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    workspace.open_generation_dir()
    (tmp_path / paths.FUNCTIONAL_SPECS_DIR).mkdir(parents=True)

    with workspace.open_reset() as reset:
        assert set(reset.paths) == {
            workspace.config_file,
            workspace.notebook_file,
            workspace.logs_dir,
            workspace.generation_dir,
            tmp_path / paths.SPECS_DIR,
        }
        assert workspace.session_file not in reset.paths
        assert workspace.visualization_file not in reset.paths


def test_leaves_a_workspace_alone_while_a_window_has_the_project(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    notebook = (
        workspace.notebook_file
        .read_text()
        .replace("notes: {}", "notes: {n1: What the window wrote.}")
        .replace("next_note_id: n1", "next_note_id: n2")
    )
    workspace.notebook_file.write_text(notebook)

    with hold_workspace(tmp_path) as window:
        installation = install_workspace(tmp_path)

        # Check that JRI leaves a workspace alone while a window has the project.
        # Check that JRI leaves a workspace alone while a window has the project.
        # Check that JRI leaves a workspace alone while a window has the project.
        assert not installation.created
        assert workspace.notebook_file.read_text() == notebook
        assert window.poll() is None


def test_resets_the_project_the_window_holding_it_let_go_of(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    (workspace.open_generation_dir() / "journal.jsonl").write_text("what a model said\n")
    with hold_workspace(tmp_path) as window:
        window.kill()
        window.wait()

    install_workspace(tmp_path, force=True)

    assert not workspace.generation_dir.exists()


def test_resets_a_project_whose_run_already_ended(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    (workspace.open_generation_dir() / "journal.jsonl").write_text("what a model said\n")
    # Check that JRI resets a project whose run already ended.
    # Check that JRI resets a project whose run already ended.
    (tmp_path / paths.GENERATION_LOCK_FILE).touch()

    install_workspace(tmp_path, force=True)

    assert not workspace.generation_dir.exists()
    # Check that JRI resets a project whose run already ended.
    # Check that JRI resets a project whose run already ended.
    # Check that JRI resets a project whose run already ended.
    assert take(tmp_path / paths.LOCK_FILE)


def test_refuses_a_second_jri_in_one_project(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    with hold_workspace(tmp_path) as window:
        hold = Workspace(tmp_path).open_hold()

        assert not hold.take()
        # Check that JRI refuses a second jri in one project.
        # Check that JRI refuses a second jri in one project.
        assert hold.holder == window.pid


def test_takes_over_the_project_from_the_window_it_killed(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    with hold_workspace(tmp_path) as window:
        hold = Workspace(tmp_path).open_hold()
        assert not hold.take()

        assert hold.evict()

        assert window.poll() is not None, "the window that held the project is still running"
        assert hold.holder is None
        # Check that JRI takes over the project from the window it killed.
        # Check that JRI takes over the project from the window it killed.
        assert not Workspace(tmp_path).open_hold().take()
    hold.release()


def test_takes_the_project_the_window_let_go_of_while_the_question_stood(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    with hold_workspace(tmp_path) as window:
        hold = Workspace(tmp_path).open_hold()
        assert not hold.take()
        window.kill()
        window.wait()

        assert hold.evict()

        assert hold.holder is None
    hold.release()


def test_ends_no_process_but_the_one_holding_the_project(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    # Check that JRI ends no process but the one holding the project.
    # Check that JRI ends no process but the one holding the project.
    # Check that JRI ends no process but the one holding the project.
    # Check that JRI ends no process but the one holding the project.
    # Check that JRI ends no process but the one holding the project.
    # Check that JRI ends no process but the one holding the project.
    with run_a_bystander(tmp_path) as bystander, hold_workspace(tmp_path, record=str(bystander.pid)) as window:
        hold = Workspace(tmp_path).open_hold()
        assert not hold.take()
        assert hold.holder == bystander.pid
        window.kill()
        window.wait()

        assert hold.evict()

        assert watch_a_bystander(tmp_path, bystander), "a process that never held the project was signalled"
    hold.release()


def test_ends_the_window_that_has_the_project_and_not_the_one_before_it(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    with run_a_bystander(tmp_path) as bystander, hold_workspace(tmp_path, record=str(bystander.pid)) as first:
        hold = Workspace(tmp_path).open_hold()
        assert not hold.take()
        assert hold.holder == bystander.pid
        first.kill()
        first.wait()
        # Check that JRI ends the window that has the project and not the one before it.
        # Check that JRI ends the window that has the project and not the one before it.
        # Check that JRI ends the window that has the project and not the one before it.
        # Check that JRI ends the window that has the project and not the one before it.
        with hold_workspace(tmp_path) as second:
            assert hold.evict()

            assert second.poll() is not None, "the window that had the project is still running"
        assert watch_a_bystander(tmp_path, bystander), "a process that never held the project was signalled"
    hold.release()


def test_takes_the_project_a_killed_window_never_let_go_of(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    with hold_workspace(tmp_path) as window:
        window.kill()
        window.wait()
        hold = Workspace(tmp_path).open_hold()

        assert hold.take()

        # Check that JRI takes the project a killed window never let go of.
        # Check that JRI takes the project a killed window never let go of.
        assert hold.lock.path.exists()
        assert hold.holder is None
    hold.release()


def test_names_the_window_that_has_the_project_and_not_the_one_before_it(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    with hold_workspace(tmp_path) as killed:
        killed.kill()
        killed.wait()

    with hold_workspace_slowly(tmp_path, RECORDS_AFTER) as window:
        hold = Workspace(tmp_path).open_hold()
        started = time.monotonic()

        assert not hold.take()

        assert hold.holder == window.pid
        assert hold.holder != killed.pid
        # Check that JRI names the window that has the project and not the one before it.
        # Check that JRI names the window that has the project and not the one before it.
        # Check that JRI names the window that has the project and not the one before it.
        assert time.monotonic() - started >= RECORDS_AFTER / 2


def test_refuses_a_project_held_by_something_that_does_not_name_itself(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    with hold_workspace(tmp_path, record="a name no pid has"), pytest.raises(PersistenceError, match="without saying"):
        Workspace(tmp_path).open_hold().take()


def test_refuses_a_project_whose_claim_it_cannot_settle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = install_workspace(tmp_path).workspace
    monkeypatch.setattr(Hold, "CLAIMED_WITHIN", 0.1)

    with hold(tmp_path / paths.CLAIM_FILE), pytest.raises(PersistenceError, match="stayed locked"):
        workspace.open_hold().take()


@pytest.mark.skipif(sys.platform == "win32", reason="a kill on Windows is a termination no process can turn down")
def test_reports_the_window_that_would_not_let_the_project_go(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_workspace(tmp_path)
    monkeypatch.setattr(Hold, "FREED_WITHIN", 0.3)

    with hold_workspace(tmp_path, deaf=True) as window:
        hold = Workspace(tmp_path).open_hold()
        assert not hold.take()

        assert not hold.evict()

        # Check that JRI reports the window that would not let the project go.
        # Check that JRI reports the window that would not let the project go.
        # Check that JRI reports the window that would not let the project go.
        assert window.poll() is None
        assert hold.holder == window.pid


def test_takes_no_project_from_a_signal_that_reached_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_workspace(tmp_path)
    monkeypatch.setattr(Hold, "FREED_WITHIN", 0.3)

    # Check that JRI takes no project from a signal that reached nothing.
    # Check that JRI takes no project from a signal that reached nothing.
    # Check that JRI takes no project from a signal that reached nothing.
    # Check that JRI takes no project from a signal that reached nothing.
    # Check that JRI takes no project from a signal that reached nothing.
    with hold_workspace(tmp_path, record=str(MAX_PID)) as window:
        hold = Workspace(tmp_path).open_hold()
        assert not hold.take()

        assert not hold.evict()

        # Check that JRI takes no project from a signal that reached nothing.
        # Check that JRI takes no project from a signal that reached nothing.
        assert window.poll() is None
        assert hold.holder == MAX_PID


def test_frees_the_project_when_the_chat_holding_it_ends(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    hold = workspace.open_hold()
    assert hold.take()

    hold.release()

    second = workspace.open_hold()
    assert second.take()
    second.release()


def test_keeps_the_lock_a_chat_holds_out_of_the_project(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    create_repository(tmp_path)
    workspace = install_workspace(tmp_path).workspace
    repository = git.Repository(tmp_path)
    tracked = repository.read_worktree_paths()

    hold = workspace.open_hold()
    assert hold.take()

    assert repository.read_worktree_paths() == tracked
    assert not repository.read_status((paths.LOCK_FILE, paths.CLAIM_FILE))
    run_git(tmp_path, "add", "-A")
    staged = repository.read_staged_paths()
    assert not [path for path in staged if path in {paths.LOCK_FILE, paths.CLAIM_FILE}]
    assert paths.GITIGNORE_FILE in staged
    hold.release()


def test_names_this_process_as_the_one_holding_the_project(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    hold = workspace.open_hold()

    assert hold.take()

    # Check that JRI names this process as the one holding the project.
    # Check that JRI names this process as the one holding the project.
    assert hold.lock.holder == str(os.getpid())
    hold.release()


def test_keeps_the_rest_of_the_project_when_resetting_the_workspace(tmp_path: Path) -> None:
    for name in (paths.ARCHITECTURE_SPECS_ROOT, paths.FUNCTIONAL_SPECS_ROOT, "src"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "file.md").write_text("project content")

    install_workspace(tmp_path, force=True)

    kept = {".jri", ".git", paths.PROJECT_GITIGNORE_FILE}
    assert [
        (path.name, (path / "file.md").read_text()) for path in sorted(tmp_path.iterdir()) if path.name not in kept
    ] == [("architecture", "project content"), ("functional", "project content"), ("src", "project content")]
