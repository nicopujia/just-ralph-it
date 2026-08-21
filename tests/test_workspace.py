import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from jri.core import paths
from jri.core.conversation import Conversation
from jri.core.exceptions import PersistenceError
from jri.core.settings import Settings
from jri.core.workspace import Hold, Installation, Workspace
from jri.lib import files, git
from jri.lib.lock import Lock
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
    watch_a_window_go,
)

if TYPE_CHECKING:
    from collections.abc import Callable

# The window releases the project before an eviction signals a process. Eviction waits `Hold.SIGNALLED_AFTER`
# first, so half of that time keeps the release inside the wait.
LETS_GO_AFTER = Hold.SIGNALLED_AFTER / 2
# This is the largest pid a hold accepts as a real process. The test writes the number in full,
# so a change to the limit makes this test fail.
MAX_PID = 2147483647
# The claim stays locked until the slow holder writes its own pid.
# `Hold.take` must wait for that release. If it does not wait, it reads
# the old record of the killed holder and not the current record.
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
        "id": "t1",
        "name": tmp_path.name,
        "status": "open",
        "notes": {},
        "connections": [],
        "next_note_id": "n1",
    }
    assert list((tmp_path / paths.LOGS_DIR).iterdir()) == []


# A directory can refuse a write, and a file can stand where JRI writes a directory. Name the workspace and the
# reason. A reader of a Python traceback learns neither.
@pytest.mark.parametrize(
    ("prepare", "reason"),
    [
        (lambda root: (root / paths.WORKSPACE_DIR).write_text("", encoding="utf-8"), "File exists"),
        (lambda root: root.chmod(0o500), "Permission denied"),
    ],
    ids=["a file stands where the workspace goes", "the project refuses a write"],
)
def test_reports_a_workspace_it_could_not_write(
    tmp_path: Path, prepare: "Callable[[Path], object]", reason: str
) -> None:
    git.Repository.init(tmp_path)
    prepare(tmp_path)

    with pytest.raises(PersistenceError, match=f"Could not write the workspace at .*{paths.WORKSPACE_DIR}.*{reason}"):
        Workspace(tmp_path).install(Settings.render())

    tmp_path.chmod(0o700)


def test_commits_the_workspace_and_leaves_the_rest_of_the_project_uncommitted(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / ".DS_Store").write_bytes(b"\x00")

    installation = install_workspace(tmp_path)

    repository = git.Repository(tmp_path)
    commit = repository.read_head()
    assert installation.commit == commit
    # The installation stages `INSTALLED_PATHS`. Write the paths here in full,
    # because a test that reads the same tuple accepts every path added to it.
    assert set(repository.read_tree(commit)) == {".jri/settings.yaml", ".jri/.gitignore", ".jri/notebook.yaml"}
    # The project belongs to the user. JRI commits only the workspace files
    # it wrote itself. The user commits everything else.
    assert {item.path for item in repository.read_status()} == {".DS_Store", ".env", "main.py"}


def test_keeps_the_worktree_directory_out_of_the_project(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    repository = git.Repository(tmp_path)

    checkout = workspace.reserve_worktree_dir()
    checkout.mkdir()
    (checkout / "main.py").write_text("print('a copy of the project')\n", encoding="utf-8")

    assert checkout == tmp_path / paths.WORKTREE_DIR
    # A run writes in this directory while Git reads the project. Git must report no file from it.
    # A later read of the project files must not find this copy of the project.
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

    # A rule written after the installation commit leaves this file modified
    # until a later turn commits it. A clone taken before that turn ignores
    # neither the lock nor the run directory.
    assert workspace.gitignore_file.read_text(encoding="utf-8") == committed
    assert not repository.read_status((paths.GITIGNORE_FILE,))


def test_leaves_the_changes_in_a_workspace_it_did_not_write_uncommitted(tmp_path: Path) -> None:
    first = install_workspace(tmp_path)
    (tmp_path / paths.SETTINGS_FILE).write_text("# The settings the user changed.\n", encoding="utf-8")

    second = install_workspace(tmp_path)

    # This installation wrote nothing, so it commits nothing. The user
    # made the change, and the user commits it.
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

    # The reset wrote the same content into every workspace file.
    # Git records no change, and the first commit stays the head.
    assert forced.commit is None
    assert git.Repository(tmp_path).read_head() == installed.commit


# Git writes the files in a run worktree read-only, and Windows refuses to remove such a file. A reset that
# fails there replaces only part of the project, and the user can never reset it. The reset must replace the
# paths it can remove, keep the other paths, and report them.
@pytest.mark.skipif(
    sys.platform == "win32", reason="a directory that refuses a write is an access list `chmod` cannot write"
)
def test_starts_the_project_over_when_a_path_it_replaces_cannot_be_removed(tmp_path: Path) -> None:
    workspace = install_workspace(tmp_path).workspace
    worktree = workspace.reserve_worktree_dir()
    worktree.mkdir()
    (worktree / "main.py").write_bytes(b"print('a copy of the project')\n")
    worktree.chmod(0o500)

    try:
        install_workspace(tmp_path, force=True)
    finally:
        worktree.chmod(0o700)

    assert (tmp_path / paths.SETTINGS_FILE).read_text(encoding="utf-8") == Settings.render()
    assert (worktree / "main.py").exists(), "a path JRI could not remove is a path it leaves"


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

    # Git refuses a partial commit during a merge, and the user finishes
    # the merge. The workspace files wait for the commit the user makes next.
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

    # A cherry-pick, a rebase, and a revert make a conflict, but they write no merge record. The index belongs
    # to the user until the user settles the conflict. The workspace files stay out of the index, and they wait
    # for the commit the user makes next.
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

    # Only a detached HEAD can reach a commit made off a branch.
    # A checkout back to the branch loses that commit and the workspace with it.
    assert installation.commit is None
    assert repository.read_head() == head
    assert (tmp_path / paths.SETTINGS_FILE).read_text(encoding="utf-8") == Settings.render()


def test_writes_no_ignore_file_into_the_project(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")

    installation = install_workspace(tmp_path)

    # The project root belongs to the user, in a repository JRI made and in
    # one JRI found. Only the workspace directory holds JRI ignore rules.
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

    # A Git process that stops during the setup must leave no incomplete
    # workspace or repository. A later attempt reads such a directory as an
    # installation that is already complete.
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


# A project can have a workspace and no repository. The user removes `.git`, or copies the project out of one
# project tree into a plain directory. The installation makes the repository, and it must commit the workspace
# into it. A clone of that new repository gets the ignore rules only from this commit.
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
    # A hold, a run, and a reserved worktree each make this directory before an installation writes into it.
    # The settings file, and not the directory, tells JRI that a workspace is there.
    (tmp_path / paths.WORKSPACE_DIR).mkdir()

    installation = install_workspace(tmp_path)

    assert installation.created
    assert (tmp_path / paths.SETTINGS_FILE).read_text(encoding="utf-8") == Settings.render()


def test_starts_the_workspace_over_when_initialization_is_forced(tmp_path: Path) -> None:
    notebook = {
        "id": "t1",
        "name": "Acme",
        "status": "open",
        "notes": {"n1": "Keep this note."},
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
        "id": "t1",
        "name": tmp_path.name,
        "status": "open",
        "notes": {},
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
        ("t1", tmp_path.name)
    ]
    assert conversation.workspace.notebook_file == base_dir / "notebook.yaml"
    assert settings_file.read_text(encoding="utf-8") == Settings.render()
    assert not conversation.workspace.session_file.exists()
    assert not (base_dir / "visualization.html").exists()
    assert not (base_dir / "specs").exists()
    assert not (base_dir / "logs" / "old.log").exists()
    # The reset paths include the files the ignore file lists, but never the
    # ignore file itself. A forced reset must keep a rule the file already has.
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
    # A local ignore rule keeps the run directory out of this clone only.
    # JRI commits the rule, so every clone excludes the same directory.
    assert paths.GITIGNORE_FILE in staged


def test_puts_back_a_run_directory_rule_something_replaced(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    create_repository(tmp_path)
    workspace = install_workspace(tmp_path).workspace
    repository = git.Repository(tmp_path)
    (workspace.open_generation_dir() / "journal.jsonl").write_text("what a model said\n", encoding="utf-8")
    # This rule names one file below the run directory, and not the directory itself. A rule is a complete line.
    # A file that only mentions the directory name has no rule for that directory.
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
    # A user edit, a merge, or a checkout of an older commit can remove the rules the hold needs from this file.
    # This rule names a file beside the claim, and not the claim itself, because a rule is a complete line.
    workspace.gitignore_file.write_text("/lock.claim.old\n", encoding="utf-8")

    hold = workspace.open_hold()
    assert hold.take()

    # The hold files belong to a project that a window has now. If Git reads them as project content,
    # it commits the hold of a window that is still open.
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


# The operating system frees the lock of a window that ends, and Windows needs a short time to do it.
# The test waits for that release, as `Hold.evict` does. It does not read the lock immediately after the
# window ends.
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
    # A forced reset takes the project hold, so it runs alone.
    # It must release that hold at the end, or no later chat can open.
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

        assert watch_a_window_go(window), "the window that held the project is still running"
        assert hold.holder is None
        # Eviction returns only after this process takes the hold.
        # This confirms that the hold has the project now. An eviction that
        # only ends the other window is not sufficient.
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

    # The window records the pid of a bystander, and not its own pid. The test ends the window and waits for its
    # lock. The project is free before eviction runs. Eviction must not use that old record to choose the
    # process to signal.
    with run_a_bystander(tmp_path) as bystander, hold_workspace(tmp_path, record=str(bystander.pid)) as window:
        hold = Workspace(tmp_path).open_hold()
        assert not hold.take()
        assert hold.holder == bystander.pid
        end_a_window(tmp_path, window)

        assert hold.evict()

        assert watch_a_bystander(tmp_path, bystander), "a process that never held the project was signalled"
    hold.release()


# The lock of a window that the operating system ended becomes free without a signal, and Windows needs a short
# time to do it. The record still names the window that ended, and the operating system can give that pid to
# another process. An eviction that signals the recorded pid immediately ends that other process.
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
        # The operating system can give the pid of a process that ended to a
        # new process. Eviction must signal the window that has the project
        # when eviction runs, and not a pid it read earlier.
        with hold_workspace(tmp_path) as second:
            assert hold.evict()

            assert watch_a_window_go(second), "the window that had the project is still running"
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

        # The slow window leaves the pid of the killed window in the file until it writes its own pid.
        # Only a read that waited for the claim names the window that is alive.
        assert hold.holder == window.pid
        assert hold.holder != killed.pid


# A user must be able to read afterwards that two windows wanted one project.
# JRI writes a record that names the window that kept the project.
def test_writes_down_the_window_that_refused_the_project(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    install_workspace(tmp_path)

    with hold_workspace(tmp_path) as window, caplog.at_level(logging.INFO, logger="jri"):
        assert not Workspace(tmp_path).open_hold().take()

    record = next(record for record in caplog.records if record.message.startswith("hold_refused"))
    assert record.message == f"hold_refused holder={window.pid}"


def test_names_the_window_that_holds_the_project_now(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    with hold_workspace(tmp_path) as window:
        assert Workspace(tmp_path).open_hold().find_holder() == window.pid


# The record of a window stays in the lock file after that window ends.
# Only the operating system tells JRI if that window is alive.
def test_names_no_window_when_the_one_that_held_the_project_left(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    with hold_workspace(tmp_path) as window:
        end_a_window(tmp_path, window)

    assert Workspace(tmp_path).open_hold().find_holder() is None

    # The read takes the lock to find that out. A read that keeps the lock stops every later window from
    # taking the project.
    assert take(tmp_path / paths.LOCK_FILE)


# The window that takes the project next reads this record. A read that writes its own pid over the record makes
# that next window read a process that never held the project.
def test_keeps_the_record_of_the_window_that_held_the_project(tmp_path: Path) -> None:
    install_workspace(tmp_path)
    lock_file = tmp_path / paths.LOCK_FILE
    with hold_workspace(tmp_path) as window:
        end_a_window(tmp_path, window)
    recorded = Lock(lock_file).holder

    assert Workspace(tmp_path).open_hold().find_holder() is None

    assert Lock(lock_file).holder == recorded


def test_writes_no_lock_when_no_window_ever_held_the_project(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    assert Workspace(tmp_path).open_hold().find_holder() is None

    assert not (tmp_path / paths.LOCK_FILE).exists()


def test_refuses_a_project_held_by_something_that_does_not_name_itself(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    with hold_workspace(tmp_path, record="a name no pid has"), pytest.raises(PersistenceError, match="without saying"):
        Workspace(tmp_path).open_hold().take()


def test_refuses_a_project_held_by_a_number_no_process_can_wear(tmp_path: Path) -> None:
    install_workspace(tmp_path)

    # A number above the largest pid reaches no process. A signal to that number fails, and `Hold` cannot undo
    # it. `Hold` must refuse the record before it signals anything.
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
    # An eviction waits before it signals, and then waits again for the operating system. The test gives it both
    # waits, or the deadline comes before eviction asks the window to end.
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

        # The operating system can give the pid of a window that ends to another process. A second signal to
        # that pid reaches the other process.
        assert read_requests_to_go(tmp_path) == 1


def test_takes_no_project_from_a_signal_that_reached_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_workspace(tmp_path)
    monkeypatch.setattr(Hold, "SIGNALLED_AFTER", 0.0)
    monkeypatch.setattr(Hold, "FREED_WITHIN", 0.3)

    # `MAX_PID` is the largest pid `Hold` accepts as real, but no process
    # has it. A signal to that pid must fail without an error, and
    # eviction must continue.
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
