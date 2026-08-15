import os
import sys
from pathlib import Path

import pytest
import yaml

from jri.core import paths
from jri.core.conversation import Conversation
from jri.core.exceptions import PersistenceError
from jri.core.settings import Settings
from jri.core.workspace import Hold, Installation, Workspace
from jri.lib import files, git
from tests.conftest import CreateRepository, RunGit
from tests.doubles.acceptance import ROOT_QUESTION, WINDOW_MARKER, install_a_killing_git
from tests.doubles.lock import hold, take
from tests.doubles.openai import FakeClient
from tests.doubles.settings import build_settings
from tests.doubles.workspace import (
    end_a_window,
    hold_workspace,
    hold_workspace_briefly,
    hold_workspace_slowly,
    install_workspace,
    read_requests_to_go,
    run_a_bystander,
    watch_a_bystander,
)

# A window lets the project go well inside the wait a takeover gives the operating system before it signals.
LETS_GO_AFTER = Hold.SIGNALLED_AFTER / 2
# The largest pid a hold reads as a real process. The test writes it out, so a change to the bound shows here as
# a failure.
MAX_PID = 2147483647
# The claim stays locked until the slow holder writes its own pid.
# `Hold.take` must wait on that release, or it could read the killed
# holder's stale record instead of the current one.
RECORDS_AFTER = 0.4


def test_initializes_a_workspace_ready_to_use(tmp_path: Path) -> None:
    installation = install_workspace(tmp_path)

    assert installation == Installation(
        Workspace(tmp_path), created=True, repository_created=True, commit=git.Repository(tmp_path).read_head()
    )
    assert installation.workspace.directory == tmp_path / paths.WORKSPACE_DIR
    assert installation.workspace.settings_file == tmp_path / paths.SETTINGS_FILE
    assert (tmp_path / paths.SETTINGS_FILE).read_text(encoding="utf-8") == Settings.render()
    assert (tmp_path / paths.GITIGNORE_FILE).read_text(encoding="utf-8") == (
        "session.json\nlogs\nvisualization.html\n/lock\n/lock.claim\n/generation/\n/worktree/\n"
    )
    assert yaml.safe_load((tmp_path / paths.NOTEBOOK_FILE).read_text(encoding="utf-8")) == {
        "topics": [{"id": "t1", "name": "Project overview", "status": "open", "notes": {}}],
        "connections": [],
        "next_note_id": "n1",
    }
    assert list((tmp_path / paths.LOGS_DIR).iterdir()) == []


def test_commits_the_workspace_and_leaves_the_rest_of_the_project_uncommitted(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / ".DS_Store").write_bytes(b"\x00")

    installation = install_workspace(tmp_path)

    repository = git.Repository(tmp_path)
    commit = repository.read_head()
    assert installation.commit == commit
    # The installation stages `INSTALLED_PATHS`.
    # Write the paths here in full: a test that reads the same tuple accepts every path added to it.
    assert set(repository.read_tree(commit)) == {".jri/settings.yaml", ".jri/.gitignore", ".jri/notebook.yaml"}
    # The project belongs to the user. Only the workspace files JRI wrote
    # itself are in its commit; everything else waits for the user.
    assert {item.path for item in repository.read_status()} == {".DS_Store", ".env", "main.py"}


def test_keeps_the_worktree_directory_out_of_the_project(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    repository = git.Repository(tmp_path)

    checkout = workspace.reserve_worktree_dir()
    checkout.mkdir()
    (checkout / "main.py").write_text("print('a copy of the project')\n", encoding="utf-8")

    assert checkout == tmp_path / paths.WORKTREE_DIR
    # A run works in this directory while Git reads the project. Git reports nothing it holds, and no later read
    # of the project files finds a copy of the project among them.
    assert repository.read_status() == ()
    assert paths.WORKTREE_DIR not in "\n".join(repository.read_worktree_paths())


def test_commits_every_ignore_rule_a_chat_and_a_run_use(tmp_path: Path) -> None:
    installation = install_workspace(tmp_path)
    workspace = installation.workspace
    repository = git.Repository(tmp_path)
    assert installation.commit is not None
    committed = repository.read_file(installation.commit, paths.GITIGNORE_FILE).decode()

    hold = workspace.open_hold()
    assert hold.take()
    workspace.open_generation_dir()
    hold.release()

    # A rule written after the installation commit would leave this file
    # modified until another turn committed it, and a clone taken before
    # that turn would ignore neither the lock nor the run directory.
    assert workspace.gitignore_file.read_text(encoding="utf-8") == committed
    assert not repository.read_status((paths.GITIGNORE_FILE,))


def test_leaves_the_changes_in_a_workspace_it_did_not_write_uncommitted(tmp_path: Path) -> None:
    first = install_workspace(tmp_path)
    (tmp_path / paths.SETTINGS_FILE).write_text("# The settings the user changed.\n", encoding="utf-8")

    second = install_workspace(tmp_path)

    # This installation wrote nothing, so it has nothing to commit. The
    # change is the user's, and they commit it when they want it kept.
    assert second.commit is None
    assert git.Repository(tmp_path).read_head() == first.commit


def test_commits_the_workspace_a_forced_start_over_replaced(tmp_path: Path, run_git: RunGit) -> None:
    install_workspace(tmp_path)
    (tmp_path / paths.SETTINGS_FILE).write_text("custom settings\n", encoding="utf-8")
    run_git(tmp_path, "commit", "-qam", "custom settings")

    installation = install_workspace(tmp_path, force=True)

    repository = git.Repository(tmp_path)
    commit = repository.read_head()
    assert installation.commit == commit
    assert repository.read_file(commit, paths.SETTINGS_FILE).decode() == Settings.render()


def test_commits_nothing_when_a_forced_start_over_changed_nothing(tmp_path: Path) -> None:
    installed = install_workspace(tmp_path)

    forced = install_workspace(tmp_path, force=True)

    # The reset rewrote every workspace file with what it already held,
    # so Git has nothing to record and the first commit still stands.
    assert forced.commit is None
    assert git.Repository(tmp_path).read_head() == installed.commit


def test_leaves_the_workspace_uncommitted_during_a_merge(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path)
    run_git(tmp_path, "checkout", "-qb", "other")
    (tmp_path / "README.md").write_bytes(b"# Other\n")
    run_git(tmp_path, "commit", "-qam", "other")
    run_git(tmp_path, "checkout", "-q", "-")
    (tmp_path / "README.md").write_bytes(b"# First\n")
    run_git(tmp_path, "commit", "-qam", "first")
    run_git(tmp_path, "merge", "other", check=False)
    head = repository.read_head()

    installation = install_workspace(tmp_path)

    # Git refuses a partial commit here, and the merge is the user's to
    # finish. The workspace still stands for the commit they make next.
    assert installation.commit is None
    assert repository.read_head() == head
    assert (tmp_path / paths.SETTINGS_FILE).read_text(encoding="utf-8") == Settings.render()


def test_stages_nothing_while_the_user_settles_a_conflicted_cherry_pick(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path)
    run_git(tmp_path, "checkout", "-qb", "other")
    (tmp_path / "README.md").write_bytes(b"# Other\n")
    run_git(tmp_path, "commit", "-qam", "other")
    run_git(tmp_path, "checkout", "-q", "-")
    (tmp_path / "README.md").write_bytes(b"# First\n")
    run_git(tmp_path, "commit", "-qam", "first")
    run_git(tmp_path, "cherry-pick", "other", check=False)
    head = repository.read_head()

    installation = install_workspace(tmp_path)

    # A cherry-pick, a rebase, and a revert leave a conflict without the merge record a merge leaves. The index
    # belongs to the user until they settle it, so the workspace waits outside it for the commit they make next.
    assert installation.commit is None
    assert repository.read_head() == head
    assert not [path for path in repository.read_staged_paths() if path.startswith(paths.WORKSPACE_DIR)]
    assert (tmp_path / paths.SETTINGS_FILE).read_text(encoding="utf-8") == Settings.render()


def test_leaves_the_workspace_uncommitted_off_a_branch(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path)
    run_git(tmp_path, "checkout", "-q", "--detach")
    head = repository.read_head()

    installation = install_workspace(tmp_path)

    # A commit made off a branch is reachable only from a detached HEAD,
    # and returning to the branch would lose the workspace with it.
    assert installation.commit is None
    assert repository.read_head() == head
    assert (tmp_path / paths.SETTINGS_FILE).read_text(encoding="utf-8") == Settings.render()


def test_writes_no_ignore_file_into_the_project(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")

    installation = install_workspace(tmp_path)

    # The project root belongs to the user, in a repository JRI created
    # as much as in one it found. Only the workspace has JRI rules.
    assert installation.repository_created
    assert not (tmp_path / ".gitignore").exists()


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

    with pytest.raises(git.Error, match="went unanswered"):
        Workspace.find()
    with pytest.raises(git.Error, match="went unanswered"):
        install_workspace(nested)

    # A Git that dies mid-setup must not leave a half-made workspace or
    # repository behind for a retry to mistake as already initialized.
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
    assert (nested / paths.SETTINGS_FILE).exists()


def test_creates_the_working_directory_when_it_is_missing(tmp_path: Path) -> None:
    missing = tmp_path / "new" / "project"

    installation = install_workspace(missing)

    assert installation.repository_created
    assert installation.workspace.directory == missing / paths.WORKSPACE_DIR
    assert (missing / paths.SETTINGS_FILE).exists()
    assert git.find_root(missing) == missing.resolve()


# A project can hold a workspace and no repository: a `.git` removed, or a copy taken out of one project tree and
# put in a plain directory. The installation makes the repository, thus it must commit the workspace into it.
# A clone of that new repository gets the ignore rules only from this commit.
def test_commits_the_workspace_it_finds_into_the_repository_it_makes_for_it(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    files.remove_directory(tmp_path / ".git")

    installation = install_workspace(tmp_path)

    assert not installation.created
    assert installation.repository_created
    assert installation.commit == git.Repository(tmp_path).read_head()
    assert not git.Repository(tmp_path).read_status(list(paths.INSTALLED_PATHS))


def test_preserves_an_existing_workspace_when_initializing_again(tmp_path: Path) -> None:
    (tmp_path / paths.WORKSPACE_DIR).mkdir()
    (tmp_path / paths.SETTINGS_FILE).write_text("custom settings\n", encoding="utf-8")
    (tmp_path / paths.GITIGNORE_FILE).write_text("custom-cache\nlogs", encoding="utf-8")

    install_workspace(tmp_path)
    installation = install_workspace(tmp_path)

    assert not installation.created
    assert (tmp_path / paths.SETTINGS_FILE).read_text(encoding="utf-8") == "custom settings\n"
    assert (tmp_path / paths.GITIGNORE_FILE).read_text(encoding="utf-8") == (
        "custom-cache\nlogs\nsession.json\nvisualization.html\n/lock\n/lock.claim\n/generation/\n/worktree/\n"
    )


def test_initializes_a_workspace_directory_that_holds_no_settings(tmp_path: Path) -> None:
    # A hold, a run, and a reserved worktree each make this directory before anything installs into it. The
    # settings, not the directory, say whether a workspace is there.
    (tmp_path / paths.WORKSPACE_DIR).mkdir()

    installation = install_workspace(tmp_path)

    assert installation.created
    assert (tmp_path / paths.SETTINGS_FILE).read_text(encoding="utf-8") == Settings.render()


def test_starts_the_workspace_over_when_initialization_is_forced(tmp_path: Path) -> None:
    notebook = {
        "topics": [{"id": "t1", "name": "Project overview", "status": "open", "notes": {"n1": "Keep this note."}}],
        "connections": [],
        "next_note_id": "n2",
    }
    install_workspace(tmp_path)
    (tmp_path / paths.SETTINGS_FILE).write_text("custom settings\n", encoding="utf-8")
    (tmp_path / paths.NOTEBOOK_FILE).write_text(yaml.safe_dump(notebook), encoding="utf-8")

    installation = install_workspace(tmp_path, force=True)

    assert not installation.created
    assert (tmp_path / paths.SETTINGS_FILE).read_text(encoding="utf-8") == Settings.render()
    assert yaml.safe_load((tmp_path / paths.NOTEBOOK_FILE).read_text(encoding="utf-8")) == {
        "topics": [{"id": "t1", "name": "Project overview", "status": "open", "notes": {}}],
        "connections": [],
        "next_note_id": "n1",
    }


def test_resets_an_invalid_workspace_when_forced(tmp_path: Path) -> None:
    base_dir = tmp_path / ".jri"
    base_dir.mkdir()
    settings_file = base_dir / "settings.yaml"
    settings_file.write_text("custom settings_file", encoding="utf-8")
    (base_dir / ".gitignore").write_text("custom-cache\n", encoding="utf-8")
    (base_dir / "notebook.yaml").write_text(": invalid yaml", encoding="utf-8")
    (base_dir / "session.json").write_text("not json", encoding="utf-8")
    (base_dir / "visualization.html").write_text("old graph", encoding="utf-8")
    (base_dir / "logs").mkdir()
    (base_dir / "logs" / "old.log").write_text("old log", encoding="utf-8")
    (base_dir / "specs").mkdir()
    (base_dir / "specs" / "old.md").write_text("old spec", encoding="utf-8")

    install_workspace(tmp_path, force=True)
    conversation = Conversation(build_settings(FakeClient([])))

    assert conversation.restore() == []
    assert conversation.session.show_thinking_blocks is False
    assert [(topic.id, topic.name) for topic in conversation.interviewer.notebook.graph.topics] == [
        ("t1", "Project overview")
    ]
    assert conversation.workspace.notebook_file == base_dir / "notebook.yaml"
    assert settings_file.read_text(encoding="utf-8") == Settings.render()
    assert not conversation.workspace.session_file.exists()
    assert not (base_dir / "visualization.html").exists()
    assert not (base_dir / "specs").exists()
    assert not (base_dir / "logs" / "old.log").exists()
    # The reset paths never include the ignore file itself, only what it
    # lists. A forced reset must keep a rule the file already held.
    assert (base_dir / ".gitignore").read_text(encoding="utf-8") == (
        "custom-cache\nsession.json\nlogs\nvisualization.html\n/lock\n/lock.claim\n/generation/\n/worktree/\n"
    )


def test_keeps_a_run_directory_out_of_the_project(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    create_repository(tmp_path)
    workspace = install_workspace(tmp_path).workspace
    repository = git.Repository(tmp_path)
    tracked = repository.read_worktree_paths()

    (workspace.open_generation_dir() / "journal.jsonl").write_text("what a model said\n", encoding="utf-8")

    assert repository.read_worktree_paths() == tracked
    assert not repository.read_status((paths.GENERATION_DIR,))
    assert run_git(tmp_path, "check-ignore", "-v", f"{paths.GENERATION_DIR}/journal.jsonl").startswith(
        f"{paths.GITIGNORE_FILE}:"
    )
    run_git(tmp_path, "add", "-A")
    staged = repository.read_staged_paths()
    assert not [path for path in staged if path.startswith(paths.GENERATION_DIR)]
    # An ignore rule that stays local only keeps the run directory out
    # of this clone. Commit it so every clone excludes the same directory.
    assert paths.GITIGNORE_FILE in staged


def test_puts_back_a_run_directory_rule_something_replaced(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    create_repository(tmp_path)
    workspace = install_workspace(tmp_path).workspace
    repository = git.Repository(tmp_path)
    (workspace.open_generation_dir() / "journal.jsonl").write_text("what a model said\n", encoding="utf-8")
    # This names one file below the run directory and not the directory itself. A rule is a complete line, so a
    # file that only mentions the directory name still misses the rule for it.
    for rule in sorted(workspace.directory.rglob(workspace.gitignore_file.name)):
        rule.write_text("/generation/old.jsonl\n", encoding="utf-8")

    workspace.open_generation_dir()

    assert not repository.read_status((paths.GENERATION_DIR,))
    run_git(tmp_path, "add", "-A")
    assert not [path for path in repository.read_staged_paths() if path.startswith(paths.GENERATION_DIR)]


def test_puts_back_a_lock_rule_something_replaced(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    create_repository(tmp_path)
    workspace = install_workspace(tmp_path).workspace
    repository = git.Repository(tmp_path)
    # A user edit, a merge, or a checkout of an older commit can leave this file without the rules the hold needs.
    # This one names a file beside the claim and not the claim itself, and a rule is a complete line.
    workspace.gitignore_file.write_text("/lock.claim.old\n", encoding="utf-8")

    hold = workspace.open_hold()
    assert hold.take()

    # The hold files hold a live project. Git that reads them as project content would commit the hold of a
    # window that is still open.
    assert not repository.read_status((paths.LOCK_FILE, paths.CLAIM_FILE))
    run_git(tmp_path, "add", "-A")
    assert not [path for path in repository.read_staged_paths() if path in {paths.LOCK_FILE, paths.CLAIM_FILE}]
    hold.release()


def test_clears_a_run_directory_a_reset_asks_for(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    (workspace.open_generation_dir() / "journal.jsonl").write_text("what a model said\n", encoding="utf-8")

    install_workspace(tmp_path, force=True)

    assert not workspace.generation_dir.exists()


def test_refuses_a_reset_while_a_window_has_the_project(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    (workspace.open_generation_dir() / "journal.jsonl").write_text("what a model said\n", encoding="utf-8")
    workspace.draft_file.write_text("diff --git a/x b/x\n", encoding="utf-8")
    (tmp_path / paths.FUNCTIONAL_SPECS_DIR).mkdir(parents=True)
    (tmp_path / paths.FUNCTIONAL_SPECS_DIR / "behavior.md").write_text("# what the project does\n", encoding="utf-8")

    with hold_workspace(tmp_path) as window:
        with pytest.raises(PersistenceError, match=str(window.pid)):
            install_workspace(tmp_path, force=True)

        assert window.poll() is None, "the window holding the project was left to write into a workspace that went"
        assert workspace.draft_file.read_text(encoding="utf-8") == "diff --git a/x b/x\n"
        assert (workspace.generation_dir / "journal.jsonl").read_text(encoding="utf-8") == "what a model said\n"
        assert (tmp_path / paths.FUNCTIONAL_SPECS_DIR / "behavior.md").read_text(
            encoding="utf-8"
        ) == "# what the project does\n"
        assert workspace.notebook_file.exists()


def test_refuses_a_reset_while_a_run_is_still_going(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    journal = workspace.open_generation_dir() / "journal.jsonl"
    journal.write_text("what a model said\n", encoding="utf-8")

    with hold(tmp_path / paths.GENERATION_LOCK_FILE) as runner:
        with pytest.raises(PersistenceError, match="run is still going"):
            install_workspace(tmp_path, force=True)

        assert runner.poll() is None, "the run was left writing into a directory that went"
        assert journal.read_text(encoding="utf-8") == "what a model said\n"
        assert not take(tmp_path / paths.GENERATION_LOCK_FILE)


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


def test_keeps_the_project_while_a_reset_is_open(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace

    with workspace.open_reset():
        assert not take(tmp_path / paths.LOCK_FILE)

    assert take(tmp_path / paths.LOCK_FILE)


def test_names_what_a_reset_replaces_and_nothing_it_would_leave(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    workspace.open_generation_dir()
    (tmp_path / paths.FUNCTIONAL_SPECS_DIR).mkdir(parents=True)

    with workspace.open_reset() as reset:
        assert set(reset.paths) == {
            workspace.settings_file,
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
        .read_text(encoding="utf-8")
        .replace("notes: {}", "notes: {n1: What the window wrote.}")
        .replace("next_note_id: n1", "next_note_id: n2")
    )
    workspace.notebook_file.write_text(notebook, encoding="utf-8")

    with hold_workspace(tmp_path) as window:
        installation = install_workspace(tmp_path)

        assert not installation.created
        assert workspace.notebook_file.read_text(encoding="utf-8") == notebook
        assert window.poll() is None


# The window lets the project go when the operating system frees the lock it held, which Windows can take a
# moment over. Wait for that, as `Hold.evict` does, rather than reading the lock the instant the process ends.
def test_resets_the_project_the_window_holding_it_let_go_of(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    (workspace.open_generation_dir() / "journal.jsonl").write_text("what a model said\n", encoding="utf-8")
    with hold_workspace(tmp_path) as window:
        end_a_window(tmp_path, window)

    install_workspace(tmp_path, force=True)

    assert not workspace.generation_dir.exists()


def test_resets_a_project_whose_run_already_ended(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    (workspace.open_generation_dir() / "journal.jsonl").write_text("what a model said\n", encoding="utf-8")
    (tmp_path / paths.GENERATION_LOCK_FILE).touch()

    install_workspace(tmp_path, force=True)

    assert not workspace.generation_dir.exists()
    # A forced reset takes the project hold to run safely. Confirm it
    # releases that hold afterward, or no chat could open next.
    assert take(tmp_path / paths.LOCK_FILE)


def test_refuses_a_second_jri_in_one_project(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    with hold_workspace(tmp_path) as window:
        hold = Workspace(tmp_path).open_hold()

        assert not hold.take()
        assert hold.holder == window.pid


def test_takes_over_the_project_from_the_window_it_killed(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    with hold_workspace(tmp_path) as window:
        hold = Workspace(tmp_path).open_hold()
        assert not hold.take()

        assert hold.evict()

        assert window.poll() is not None, "the window that held the project is still running"
        assert hold.holder is None
        # Eviction only returns once this process's own take succeeds.
        # Confirm hold now actually holds the project, not just that it
        # killed the window that did.
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

    # The window records a bystander's pid, not its own. End the window and wait for its lock, so the project is
    # already free before eviction runs: eviction must never use that stale record to choose whom to signal.
    with run_a_bystander(tmp_path) as bystander, hold_workspace(tmp_path, record=str(bystander.pid)) as window:
        hold = Workspace(tmp_path).open_hold()
        assert not hold.take()
        assert hold.holder == bystander.pid
        end_a_window(tmp_path, window)

        assert hold.evict()

        assert watch_a_bystander(tmp_path, bystander), "a process that never held the project was signalled"
    hold.release()


# The lock of a window the operating system ended comes free without a signal, and Windows takes a moment over
# it. The record still names the window that left, and the operating system can already have given that number
# to another process. A takeover that signals it at once ends whatever wears the number now.
def test_ends_no_process_while_the_project_is_still_coming_free(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    with (
        run_a_bystander(tmp_path) as bystander,
        hold_workspace_briefly(tmp_path, LETS_GO_AFTER, record=str(bystander.pid)),
    ):
        hold = Workspace(tmp_path).open_hold()

        assert hold.evict()

        assert watch_a_bystander(tmp_path, bystander), "a process that never held the project was signalled"
    hold.release()


def test_ends_the_window_that_has_the_project_and_not_the_one_before_it(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    with run_a_bystander(tmp_path) as bystander, hold_workspace(tmp_path, record=str(bystander.pid)) as first:
        hold = Workspace(tmp_path).open_hold()
        assert not hold.take()
        assert hold.holder == bystander.pid
        end_a_window(tmp_path, first)
        # A pid can be freed and reused once its process exits. Confirm
        # eviction signals whoever holds the project when it runs, not a
        # pid read earlier and now stale.
        with hold_workspace(tmp_path) as second:
            assert hold.evict()

            assert second.poll() is not None, "the window that had the project is still running"
        assert watch_a_bystander(tmp_path, bystander), "a process that never held the project was signalled"
    hold.release()


def test_takes_the_project_a_killed_window_never_let_go_of(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    with hold_workspace(tmp_path) as window:
        end_a_window(tmp_path, window)
        hold = Workspace(tmp_path).open_hold()

        assert hold.take()

        assert hold.lock.path.exists()
        assert hold.holder is None
    hold.release()


def test_names_the_window_that_has_the_project_and_not_the_one_before_it(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    with hold_workspace(tmp_path) as killed:
        end_a_window(tmp_path, killed)

    with hold_workspace_slowly(tmp_path, RECORDS_AFTER) as window:
        hold = Workspace(tmp_path).open_hold()

        assert not hold.take()

        # The slow window leaves the killed pid in the file until it records its own.
        # Naming the live window is what a read that waited for the claim gives.
        assert hold.holder == window.pid
        assert hold.holder != killed.pid


def test_refuses_a_project_held_by_something_that_does_not_name_itself(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    with hold_workspace(tmp_path, record="a name no pid has"), pytest.raises(PersistenceError, match="without saying"):
        Workspace(tmp_path).open_hold().take()


def test_refuses_a_project_held_by_a_number_no_process_can_wear(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    # A number above the largest pid reaches no process, and a signal aimed at it fails in a way `Hold` cannot
    # take back. Turn the record down while it is still a record.
    with hold_workspace(tmp_path, record=str(MAX_PID + 1)), pytest.raises(PersistenceError, match="without saying"):
        Workspace(tmp_path).open_hold().take()


def test_refuses_a_project_whose_claim_it_cannot_settle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = install_workspace(tmp_path).workspace
    monkeypatch.setattr(Hold, "CLAIMED_WITHIN", 0.1)

    with hold(tmp_path / paths.CLAIM_FILE), pytest.raises(PersistenceError, match="stayed locked"):
        workspace.open_hold().take()


@pytest.mark.skipif(sys.platform == "win32", reason="a kill on Windows is a termination no process can turn down")
def test_reports_the_window_that_would_not_let_the_project_go(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_workspace(tmp_path)
    # A takeover waits before it signals, then waits again for the operating system. Give it both waits, or the
    # deadline arrives before the window is ever asked to go.
    monkeypatch.setattr(Hold, "SIGNALLED_AFTER", 0.0)
    monkeypatch.setattr(Hold, "FREED_WITHIN", 0.3)

    with hold_workspace(tmp_path, deaf=True) as window:
        hold = Workspace(tmp_path).open_hold()
        assert not hold.take()

        assert not hold.evict()

        assert read_requests_to_go(tmp_path), "the window was reported without being asked to go"
        assert window.poll() is None
        assert hold.holder == window.pid


@pytest.mark.skipif(sys.platform == "win32", reason="a kill on Windows is a termination no process can turn down")
def test_asks_a_window_to_let_the_project_go_one_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_workspace(tmp_path)
    monkeypatch.setattr(Hold, "SIGNALLED_AFTER", 0.0)
    monkeypatch.setattr(Hold, "FREED_WITHIN", 0.3)

    with hold_workspace(tmp_path, deaf=True):
        hold = Workspace(tmp_path).open_hold()
        assert not hold.take()

        assert not hold.evict()

        # The operating system can hand the number of a window that leaves to another process. A second request
        # to the same number would reach whatever wears it by then.
        assert read_requests_to_go(tmp_path) == 1


def test_takes_no_project_from_a_signal_that_reached_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_workspace(tmp_path)
    monkeypatch.setattr(Hold, "SIGNALLED_AFTER", 0.0)
    monkeypatch.setattr(Hold, "FREED_WITHIN", 0.3)

    # MAX_PID is the largest pid Hold accepts as real, but no process
    # holds it. A signal aimed at it must fail quietly, not derail
    # eviction.
    with hold_workspace(tmp_path, record=str(MAX_PID)) as window:
        hold = Workspace(tmp_path).open_hold()
        assert not hold.take()

        assert not hold.evict()

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

    assert hold.lock.holder == str(os.getpid())
    hold.release()


def test_keeps_the_rest_of_the_project_when_resetting_the_workspace(tmp_path: Path) -> None:
    for name in (paths.ARCHITECTURE_SPECS_ROOT, paths.FUNCTIONAL_SPECS_ROOT, "src"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "file.md").write_text("project content", encoding="utf-8")

    install_workspace(tmp_path, force=True)

    kept = {".jri", ".git"}
    assert [
        (path.name, (path / "file.md").read_text(encoding="utf-8"))
        for path in sorted(tmp_path.iterdir())
        if path.name not in kept
    ] == [("architecture", "project content"), ("functional", "project content"), ("src", "project content")]
