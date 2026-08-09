import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from jri.lib import git
from tests.conftest import CreateRepository, RunGit
from tests.doubles.acceptance import (
    HEAD_QUESTION,
    HOLD_THE_LOCK,
    KILL_THE_GIT,
    RECORD_THE_LOCKS,
    REFUSE_THE_COMMIT,
    ROOT_QUESTION,
    SIGNAL_THE_GIT,
    TAKE_THE_LOCK,
    WINDOW_MARKER,
    WORKTREE_QUESTION,
    end_the_second_command,
    install_a_killing_git,
    is_the_second_command_running,
    open_a_filter_window,
    open_a_window,
    read_git_locks,
    read_the_locks_the_window_saw,
    stale_the_filtered_path,
)

CONTEXT_FREE_PATCH = b"""\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -2 +2,2 @@
 The store keeps orders.
+The reporter renders totals.
"""
# Two hunks whose header and body disagree: one counting more lines
# than the body holds, one cut back to the single context line a write
# stopped part way through leaves. Re-derived from their bodies, the
# first places a change nobody wrote a header for and the second
# places nothing at all.
MISCOUNTED_PATCHES = (
    b"""\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,7 +1,9 @@
-# Project
+# Renamed
""",
    b"""\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 # Project
""",
)
SECTIONED_README = b"# Store\nKeeps orders.\n\n# Reporter\nKeeps orders.\n"
RENAMING_PATCH = b"""\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-# Project
+# Renamed
"""
# What a hard kill inside `git init` leaves behind, by the lock it was
# holding: the command writes the config and then HEAD, each under one,
# so the wreck is what it had written by then with that lock standing.
# Built rather than killed for, and what makes the built state the
# killed one is where Git stops in it: `rev-parse` calls neither a
# repository, and the next `init` reports `File exists` over the config
# and `cannot lock ref` over HEAD, exactly as it does over a real kill.
KILLED_INITS = (("config.lock", ("config", "HEAD")), ("HEAD.lock", ("HEAD",)))
# The signals Git is asked to stop at that a hook can end a commit
# with. `sigchain_push_common` gives SIGPIPE the same handler as these
# four, and Git ignores it over a commit, so it is the one member of
# that handler's set no commit here can be made to die of.
HANDLED_SIGNALS_A_COMMIT_DIES_OF = ("HUP", "INT", "QUIT", "TERM")


def test_rejects_a_missing_git_executable(tmp_path: Path) -> None:
    with pytest.raises(git.NotInstalledError):
        git.Repository(tmp_path, executable="missing-git-executable")


def test_refuses_to_open_a_path_outside_any_worktree(tmp_path: Path) -> None:
    with pytest.raises(git.NotRepositoryError):
        git.Repository(tmp_path)

    assert not (tmp_path / ".git").exists()


def test_initializes_a_repository_only_when_asked(tmp_path: Path) -> None:
    repository = git.Repository.init(tmp_path / "project")

    assert (tmp_path / "project" / ".git").is_dir()
    assert not repository.has_commit()


def test_keeps_the_worktree_an_existing_repository_already_has(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    create_repository(tmp_path / "repo")
    nested = tmp_path / "repo" / "packages"
    nested.mkdir()

    repository = git.Repository.init(nested)

    assert repository.path == (tmp_path / "repo").resolve()
    assert repository.has_commit()


@pytest.mark.parametrize(("lock", "unwritten"), KILLED_INITS)
def test_initializes_over_what_a_killed_initialization_left(
    tmp_path: Path, run_git: RunGit, lock: str, unwritten: tuple[str, ...]
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    run_git(root, "init", "-q")
    for name in unwritten:
        (root / ".git" / name).unlink()
    (root / ".git" / lock).touch()

    repository = git.Repository.init(root)

    assert repository.path == root.resolve()
    assert git.find_root(root) == root.resolve()
    assert read_git_locks(root) == ()


def test_leaves_the_locks_over_a_repository_that_already_exists(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")
    held = tuple(repository.path / ".git" / name for name, _ in KILLED_INITS)
    for path in held:
        path.touch()

    git.Repository.init(repository.path)

    assert read_git_locks(repository.path) == tuple(sorted(held))


def test_finds_worktree_root_from_any_subdirectory(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    nested = repository.path / "packages" / "app"
    nested.mkdir(parents=True)

    assert git.find_root(nested) == repository.path
    assert git.find_root(repository.path) == repository.path
    assert git.find_root(tmp_path) is None


def test_reads_the_files_a_revision_tracks(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    revision = repository.read_head()
    (repository.path / "README.md").write_bytes(b"second\n")

    assert repository.read_file(revision, "README.md") == b"# Project\n"
    assert repository.read_tree(revision) == {"README.md": b"# Project\n"}


def test_diffs_the_worktree_against_a_revision(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    revision = repository.read_head()
    (repository.path / "README.md").write_bytes(b"second\n")

    assert b"+second" in repository.diff(revision, paths=["README.md"])


def test_reports_changed_and_untracked_paths(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_bytes(b"second\n")
    (repository.path / "new file.txt").write_bytes(b"new\n")

    assert {(item.path, item.index, item.worktree) for item in repository.read_status()} == {
        ("README.md", " ", "M"),
        ("new file.txt", "?", "?"),
    }


def test_reads_the_status_of_the_paths_it_is_given(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "docs").mkdir()
    (repository.path / "docs" / "guide.md").write_bytes(b"# Guide\n")
    (repository.path / "README.md").write_bytes(b"second\n")

    assert repository.read_status(["docs"]) == (git.Status("docs/guide.md", "?", "?"),)
    assert repository.read_status(["missing"]) == ()


def test_reads_the_status_of_a_path_the_project_ignores(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / ".gitignore").write_bytes(b"build/\n")
    (repository.path / "build").mkdir()
    (repository.path / "build" / "report.md").write_bytes(b"# Report\n")

    assert repository.read_status(["build"]) == ()
    assert repository.read_status(["build"], ignored=True) == (git.Status("build/report.md", "!", "!"),)


def test_keeps_the_lock_that_was_standing_before_a_command_of_its_own(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    index_lock = tmp_path / ".git/index.lock"
    index_lock.touch()

    with pytest.raises(git.Error, match=r"index\.lock"):
        repository.stage(("README.md",))

    assert read_git_locks(tmp_path) == (index_lock,)


@pytest.mark.skipif(sys.platform == "win32", reason="a hook that takes a lock needs a shell and `touch`")
def test_keeps_the_lock_another_command_took_while_its_own_ran(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")

    with open_a_window(tmp_path, "past", TAKE_THE_LOCK.format(directory=tmp_path / ".git", lock="index.lock")):
        repository.commit("second", paths=("README.md",))

    assert read_git_locks(tmp_path) == (tmp_path / ".git/index.lock",)


@pytest.mark.skipif(sys.platform == "win32", reason="a hook that takes a lock needs a shell and `touch`")
def test_keeps_the_lock_another_command_took_while_a_refused_one_ran(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")
    # HEAD rather than the index, since the commit is holding the index
    # lock here and no second command could have taken that one.
    window = TAKE_THE_LOCK.format(directory=tmp_path / ".git", lock="HEAD.lock") + REFUSE_THE_COMMIT

    with open_a_window(tmp_path, "index", window), pytest.raises(git.Error):
        repository.commit("second", paths=("README.md",))

    assert read_git_locks(tmp_path) == (tmp_path / ".git/HEAD.lock",)


@pytest.mark.skipif(sys.platform == "win32", reason="a hook that signals its own Git needs a shell and `kill`")
@pytest.mark.parametrize("name", HANDLED_SIGNALS_A_COMMIT_DIES_OF)
def test_keeps_the_lock_a_running_command_holds_when_a_signal_ends_its_own_git(
    tmp_path: Path, create_repository: CreateRepository, name: str
) -> None:
    repository = create_repository(tmp_path)
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")
    window = HOLD_THE_LOCK.format(directory=tmp_path / ".git", lock="HEAD.lock") + SIGNAL_THE_GIT.format(name=name)

    with open_a_window(tmp_path, "index", window), pytest.raises(git.Error):
        repository.commit("second", paths=("README.md",))

    try:
        assert is_the_second_command_running(tmp_path)
        assert read_git_locks(tmp_path) == (tmp_path / ".git/HEAD.lock",)
        # Git's handler took its own away before the signal ended it,
        # so what the next command meets is the second command's lock
        # and nothing this one left, and what it does is say so.
        with pytest.raises(git.Error, match=r"HEAD\.lock"):
            repository.commit("second", paths=("README.md",))
    finally:
        end_the_second_command(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="a file that refuses a read is an access list `chmod` cannot write")
def test_reports_git_refusing_a_repository_whose_head_cannot_be_read(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")
    # Which locks a command of this repository's can leave is read off
    # HEAD, and a HEAD nothing can read is a repository Git refuses:
    # what the run ends over is Git's own words, not the read.
    (tmp_path / ".git/HEAD").chmod(0o000)

    try:
        with pytest.raises(git.Error, match="not a git repository"):
            repository.commit("second", paths=("README.md",))
    finally:
        (tmp_path / ".git/HEAD").chmod(0o600)


@pytest.mark.skipif(sys.platform == "win32", reason="a hook that kills its own Git needs a shell and `kill`")
@pytest.mark.parametrize("lock", ["index.lock", "HEAD.lock"])
def test_keeps_the_lock_a_running_command_holds_when_a_kill_ends_a_git_that_never_took_it(
    tmp_path: Path, create_repository: CreateRepository, lock: str
) -> None:
    repository = create_repository(tmp_path)
    # `git worktree add` locks nothing of the main worktree's, so a
    # second command has the whole of its span to take one of these in
    # and the kill below answers for none of them.
    window = HOLD_THE_LOCK.format(directory=tmp_path / ".git", lock=lock) + KILL_THE_GIT

    with open_a_window(tmp_path, "worktree", window), pytest.raises(git.Error), repository.open_worktree("HEAD"):
        pass

    try:
        assert is_the_second_command_running(tmp_path)
        assert read_git_locks(tmp_path) == (tmp_path / ".git" / lock,)
    finally:
        end_the_second_command(tmp_path)


@pytest.mark.skipif(
    sys.platform == "win32", reason="a directory that refuses a write is an access list `chmod` cannot write"
)
def test_leaves_the_lock_it_is_refused_the_removal_of(tmp_path: Path) -> None:
    directory = tmp_path / "guarded"
    directory.mkdir()
    lock = directory / "index.lock"
    lock.touch()
    directory.chmod(0o500)

    try:
        git.Locks((directory,), (directory / "index",)).release(())
    finally:
        directory.chmod(0o700)

    assert lock.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="a hook that kills its own Git needs a shell and `kill`")
@pytest.mark.parametrize("window", ["index", "branch"])
def test_frees_the_locks_the_git_it_started_died_holding(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit, window: str
) -> None:
    repository = create_repository(tmp_path)
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")

    with open_a_window(tmp_path, window, KILL_THE_GIT), pytest.raises(git.Error):
        repository.commit("second", paths=("README.md",))

    assert read_git_locks(tmp_path) == ()
    # The commit the kill cut short never landed, and the next command
    # runs as though the locks it would have met had never been made.
    assert run_git(tmp_path, "log", "--format=%s") == "initial"
    assert repository.commit("second", paths=("README.md",))
    assert run_git(tmp_path, "log", "--format=%s").splitlines() == ["second", "initial"]


@pytest.mark.skipif(sys.platform == "win32", reason="a filter that kills its own Git needs a shell and `kill`")
def test_frees_the_index_lock_the_apply_it_started_died_holding(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)

    with open_a_filter_window(tmp_path, RECORD_THE_LOCKS + KILL_THE_GIT, side="smudge"), pytest.raises(git.Error):
        repository.apply_patch(RENAMING_PATCH, index=True)

    # What the release answers for is a lock the dead apply was holding,
    # and an apply that never took one leaves the same empty `.git`.
    assert read_the_locks_the_window_saw(tmp_path) == (".git/index.lock",)
    assert read_git_locks(tmp_path) == ()
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")
    assert repository.commit("second", paths=("README.md",))


@pytest.mark.skipif(sys.platform == "win32", reason="a filter that kills its own Git needs a shell and `kill`")
def test_keeps_the_lock_a_running_command_holds_when_a_kill_ends_an_apply_that_took_none(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    # An apply that was not asked for the index never takes its lock, so
    # the one standing at the end is the second command's whole.
    window = HOLD_THE_LOCK.format(directory=tmp_path / ".git", lock="index.lock") + KILL_THE_GIT

    with open_a_filter_window(tmp_path, window, side="smudge"), pytest.raises(git.Error):
        repository.apply_patch(RENAMING_PATCH)

    try:
        assert is_the_second_command_running(tmp_path)
        assert read_git_locks(tmp_path) == (tmp_path / ".git/index.lock",)
    finally:
        end_the_second_command(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="a filter that kills its own Git needs a shell and `kill`")
def test_frees_the_index_lock_the_staging_it_started_died_holding(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")

    with open_a_filter_window(tmp_path, RECORD_THE_LOCKS + KILL_THE_GIT, side="clean"), pytest.raises(git.Error):
        repository.stage(("README.md",))

    # What the release answers for is a lock the dead staging was
    # holding, and a command that never took one leaves the same empty
    # `.git`, so what the window saw is what tells those two apart.
    assert read_the_locks_the_window_saw(tmp_path) == (".git/index.lock",)
    assert read_git_locks(tmp_path) == ()
    assert repository.commit("second", paths=("README.md",))


@pytest.mark.skipif(sys.platform == "win32", reason="a filter that kills its own Git needs a shell and `kill`")
def test_keeps_the_lock_over_head_a_running_command_holds_when_a_kill_ends_a_staging(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")
    # A commit is the one command here that moves HEAD and the branch,
    # so a staging leaves both free for the whole of its span however
    # firmly it is holding the index, and the lock over HEAD standing at
    # the end is the second command's whole.
    window = HOLD_THE_LOCK.format(directory=tmp_path / ".git", lock="HEAD.lock") + KILL_THE_GIT

    with open_a_filter_window(tmp_path, window, side="clean"), pytest.raises(git.Error):
        repository.stage(("README.md",))

    try:
        assert is_the_second_command_running(tmp_path)
        assert read_git_locks(tmp_path) == (tmp_path / ".git/HEAD.lock",)
    finally:
        end_the_second_command(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="a filter that kills its own Git needs a shell and `kill`")
def test_keeps_the_lock_a_running_command_holds_when_a_kill_ends_a_read(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    # A read of Git's takes the index lock at no point at all, so a
    # second command has the whole of its span to take that lock in and
    # the kill below answers for none of it.
    stale_the_filtered_path(tmp_path)
    window = HOLD_THE_LOCK.format(directory=tmp_path / ".git", lock="index.lock") + KILL_THE_GIT

    with open_a_filter_window(tmp_path, window, side="clean"), pytest.raises(git.Error):
        repository.read_status()

    try:
        assert is_the_second_command_running(tmp_path)
        assert read_git_locks(tmp_path) == (tmp_path / ".git/index.lock",)
    finally:
        end_the_second_command(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="a filter that kills its own Git needs a shell and `kill`")
def test_keeps_the_lock_a_running_command_holds_when_a_kill_ends_a_commit_of_no_named_paths(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")
    repository.stage(("README.md",))
    # A commit of everything already staged renames the index it
    # refreshed over the file before `pre-commit` runs, so the index is
    # free for the whole of the hook and the kill below answers for
    # none of it.
    window = HOLD_THE_LOCK.format(directory=tmp_path / ".git", lock="index.lock") + KILL_THE_GIT

    with open_a_window(tmp_path, "index", window), pytest.raises(git.Error):
        repository.commit("second")

    try:
        assert is_the_second_command_running(tmp_path)
        assert read_git_locks(tmp_path) == (tmp_path / ".git/index.lock",)
    finally:
        end_the_second_command(tmp_path)


def test_leaves_the_index_alone_where_a_read_would_only_be_refreshing_it(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    # The index caches what `stat` said about each file it holds, so a
    # tracked file whose timestamp no longer matches is one Git writes
    # a refreshed index for -- under the lock, and for a read.
    os.utime(repository.path / "README.md", (0, 0))
    index = repository.path / ".git/index"
    before = index.stat()

    repository.read_status()

    after = index.stat()
    assert (after.st_mtime_ns, after.st_ino) == (before.st_mtime_ns, before.st_ino)


def test_reads_the_status_of_paths_a_repository_without_commits_may_not_hold(tmp_path: Path) -> None:
    repository = git.Repository.init(tmp_path / "project")
    (repository.path / "notes.md").write_bytes(b"# Notes\n")

    assert repository.read_status(["notes.md"]) == (git.Status("notes.md", "?", "?"),)
    assert repository.read_status(["missing"]) == ()


def test_reports_a_repository_with_unmerged_paths(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    base = repository.read_head()
    (repository.path / "README.md").write_bytes(b"theirs\n")
    repository.stage(["README.md"])
    theirs = repository.commit("jri: write theirs")
    run_git(repository.path, "reset", "-q", "--hard", base)
    (repository.path / "README.md").write_bytes(b"ours\n")
    repository.stage(["README.md"])
    ours = repository.commit("jri: write ours")

    run_git(repository.path, "read-tree", "-m", base, ours, theirs)

    assert repository.has_conflicts()


def test_reports_a_repository_without_unmerged_paths(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_bytes(b"second\n")
    repository.stage(["README.md"])

    assert not repository.has_conflicts()


def test_reports_whether_a_commit_would_land_on_a_branch(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")

    assert repository.is_on_branch()

    run_git(repository.path, "checkout", "-q", "--detach", "HEAD")

    assert not repository.is_on_branch()


def test_reports_a_repository_without_commits_as_being_on_a_branch(tmp_path: Path) -> None:
    repository = git.Repository.init(tmp_path / "project")

    assert repository.is_on_branch()


def test_moves_staged_paths_to_the_index_side_of_the_status(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_bytes(b"second\n")
    (repository.path / "new file.txt").write_bytes(b"new\n")

    repository.stage(["README.md", "new file.txt"])

    assert {(item.path, item.index, item.worktree) for item in repository.read_status()} == {
        ("README.md", "M", " "),
        ("new file.txt", "A", " "),
    }


def test_records_every_trailer_it_is_given(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_bytes(b"second\n")
    repository.stage(["README.md"])

    commit = repository.commit("jri: test", ["Co-authored-by: Test Person <test@example.com>", "JRI-Test: accepted"])

    assert run_git(repository.path, "show", "-s", "--format=%B", commit) == (
        "jri: test\n\nCo-authored-by: Test Person <test@example.com>\nJRI-Test: accepted"
    )


def test_commits_staged_paths_without_any_trailer(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_bytes(b"second\n")
    repository.stage(["README.md"])

    commit = repository.commit("jri: test")

    assert run_git(repository.path, "show", "-s", "--format=%B", commit) == "jri: test"


def test_finds_the_last_commit_whose_message_holds_the_text(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_bytes(b"second\n")
    repository.stage(["README.md"])
    marked = repository.commit("jri: test", ["JRI-Test: accepted"])
    (repository.path / "README.md").write_bytes(b"third\n")
    run_git(repository.path, "commit", "-qam", "docs: a commit of the user's own")

    assert repository.find_commit("JRI-Test: accepted") == marked
    assert repository.find_commit("JRI-Test: rejected") is None


def test_commits_only_the_paths_it_names(tmp_path: Path, create_repository: CreateRepository, run_git: RunGit) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "notes.md").write_bytes(b"# Notes\n")
    repository.stage(["notes.md"], intent_to_add=True)
    (repository.path / "staged.txt").write_bytes(b"staged\n")
    repository.stage(["staged.txt"])
    (repository.path / "staged.txt").write_bytes(b"edited after staging\n")
    (repository.path / "README.md").write_bytes(b"second\n")
    (repository.path / "untracked.txt").write_bytes(b"untracked\n")

    commit = repository.commit("jri: add notes", paths=["notes.md"])

    assert repository.read_tree(commit) == {"README.md": b"# Project\n", "notes.md": b"# Notes\n"}
    assert run_git(repository.path, "show", ":staged.txt") == "staged"
    assert (repository.path / "staged.txt").read_text() == "edited after staging\n"
    assert {(item.path, item.index, item.worktree) for item in repository.read_status()} == {
        ("staged.txt", "A", "M"),
        ("README.md", " ", "M"),
        ("untracked.txt", "?", "?"),
    }


def test_commits_named_paths_into_a_repository_without_commits(tmp_path: Path) -> None:
    repository = git.Repository.init(tmp_path / "project")
    (repository.path / "notes.md").write_bytes(b"# Notes\n")
    (repository.path / "secrets.env").write_bytes(b"SECRET=1\n")
    repository.stage(["notes.md"], intent_to_add=True)

    commit = repository.commit("jri: add notes", paths=["notes.md"])

    assert repository.read_tree(commit) == {"notes.md": b"# Notes\n"}
    assert repository.read_status() == (git.Status("secrets.env", "?", "?"),)


def test_stages_only_the_intent_to_add_a_path(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "notes.md").write_bytes(b"# Notes\n")
    (repository.path / "other.md").write_bytes(b"# Other\n")
    repository.stage(["other.md"])

    repository.stage(["notes.md"], intent_to_add=True)

    assert repository.read_status() == (git.Status("notes.md", " ", "A"), git.Status("other.md", "A", " "))
    commit = repository.commit("jri: add the other note")
    assert run_git(repository.path, "show", "--format=", "--name-only", commit).splitlines() == ["other.md"]


def test_reads_the_paths_the_index_holds(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "notes.md").write_bytes(b"# Notes\n")
    (repository.path / "untracked.md").write_bytes(b"# Untracked\n")
    repository.stage(["notes.md"], intent_to_add=True)

    assert repository.read_staged_paths() == ("README.md", "notes.md")
    assert repository.read_staged_paths(["notes.md", "untracked.md"]) == ("notes.md",)


def test_stages_a_path_the_project_ignores(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / ".gitignore").write_bytes(b"notes.md\n")
    (repository.path / "notes.md").write_bytes(b"# Notes\n")

    repository.stage(["notes.md"], force=True)

    assert repository.read_status(["notes.md"]) == (git.Status("notes.md", "A", " "),)


def test_unstages_the_paths_it_is_given(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "notes.md").write_bytes(b"# Notes\n")
    (repository.path / "kept.md").write_bytes(b"# Kept\n")
    repository.stage(["notes.md", "kept.md"])

    repository.unstage(["notes.md"])

    assert repository.read_status() == (git.Status("kept.md", "A", " "), git.Status("notes.md", "?", "?"))


def test_unstages_the_paths_of_a_repository_without_commits(tmp_path: Path) -> None:
    repository = git.Repository.init(tmp_path / "project")
    (repository.path / "notes.md").write_bytes(b"# Notes\n")
    repository.stage(["notes.md"], intent_to_add=True)

    repository.unstage(["notes.md"])

    assert repository.read_status() == (git.Status("notes.md", "?", "?"),)


def test_restores_only_the_paths_it_is_given(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "notes.md").write_bytes(b"# Notes\n")
    repository.stage(["notes.md"])
    repository.commit("docs: add notes")
    (repository.path / "notes.md").unlink()
    (repository.path / "README.md").write_bytes(b"# Edited\n")

    repository.restore("HEAD", ["notes.md"])

    assert (repository.path / "notes.md").read_bytes() == b"# Notes\n"
    assert (repository.path / "README.md").read_bytes() == b"# Edited\n"


def test_reverses_a_patch_it_applied(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    patch = b"""\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-# Project
+# Renamed
diff --git a/docs/notes.md b/docs/notes.md
new file mode 100644
--- /dev/null
+++ b/docs/notes.md
@@ -0,0 +1 @@
+# Notes
"""
    repository.apply_patch(patch)

    repository.apply_patch(patch, reverse=True)

    assert (repository.path / "README.md").read_bytes() == b"# Project\n"
    assert not (repository.path / "docs").exists()
    assert repository.read_status() == ()


def test_reports_renames_with_their_original_path(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    run_git(repository.path, "mv", "README.md", "docs.md")
    (repository.path / "untracked.md").write_bytes(b"new\n")

    status = repository.read_status()

    assert [(item.path, item.index, item.original_path) for item in status] == [
        ("docs.md", "R", "README.md"),
        ("untracked.md", "?", None),
    ]


def test_applies_a_patch_to_the_worktree_and_the_index(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_bytes(b"updated\n")
    patch = repository.diff("HEAD", paths=["README.md"])
    (repository.path / "README.md").write_bytes(b"# Project\n")
    repository.stage(["README.md"])

    repository.apply_patch(patch, index=True)

    assert (repository.path / "README.md").read_text() == "updated\n"
    assert repository.read_status()[0].index == "M"


def test_rejects_a_patch_that_does_not_apply(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_bytes(b"updated\n")
    patch = repository.diff("HEAD", paths=["README.md"])

    with pytest.raises(git.Error):
        repository.apply_patch(patch)


def test_checks_a_patch_without_applying_it(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    patch = b"""\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-# Project
+# Renamed
"""

    repository.apply_patch(patch, check=True)

    assert (repository.path / "README.md").read_bytes() == b"# Project\n"
    assert repository.read_status() == ()
    with pytest.raises(git.Error):
        repository.apply_patch(patch, check=True, reverse=True)


def test_applies_a_patch_whose_hunk_carries_no_trailing_context(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_bytes(b"# Project\nThe store keeps orders.\nEverything runs offline.\n")

    repository.apply_patch(CONTEXT_FREE_PATCH, zero_context=True)

    assert (repository.path / "README.md").read_text() == (
        "# Project\nThe store keeps orders.\nThe reporter renders totals.\nEverything runs offline.\n"
    )


def test_rejects_a_patch_whose_hunk_carries_no_trailing_context(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_bytes(b"# Project\nThe store keeps orders.\nEverything runs offline.\n")

    with pytest.raises(git.Error):
        repository.apply_patch(CONTEXT_FREE_PATCH)


def test_rejects_a_context_free_hunk_quoting_a_line_the_file_does_not_hold(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_bytes(b"# Project\nThe store keeps orders.\nEverything runs offline.\n")
    patch = b"""\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -2 +2 @@
-The store never kept orders.
+The reporter renders totals.
"""

    with pytest.raises(git.Error):
        repository.apply_patch(patch, zero_context=True)


def test_places_a_context_free_hunk_at_the_line_its_header_names(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_bytes(SECTIONED_README)
    patch = b"""\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -2 +2,2 @@
 Keeps orders.
+Renders totals.
"""

    repository.apply_patch(patch, zero_context=True)

    assert (repository.path / "README.md").read_text() == (
        "# Store\nKeeps orders.\nRenders totals.\n\n# Reporter\nKeeps orders.\n"
    )


def test_applies_a_patch_below_a_directory(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    patch = b"""\
diff --git a/notes.md b/notes.md
new file mode 100644
--- /dev/null
+++ b/notes.md
@@ -0,0 +1 @@
+# Notes
"""

    repository.apply_patch(patch, index=True, directory="docs/internal")

    assert (repository.path / "docs/internal/notes.md").read_text() == "# Notes\n"
    assert repository.read_status() == (git.Status("docs/internal/notes.md", "A", " "),)


def test_opens_a_detached_worktree_at_the_requested_revision(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")

    with repository.open_worktree() as worktree:
        assert worktree.path.exists()
        assert worktree.read_head() == repository.read_head()
        assert run_git(worktree.path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def test_snapshots_the_working_tree_when_no_revision_is_given(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_bytes(b"uncommitted edit\n")
    (repository.path / "docs").mkdir()
    (repository.path / "docs" / "new.md").write_bytes(b"# New\n")
    (repository.path / ".gitignore").write_bytes(b"*.log\n")
    (repository.path / "noise.log").write_bytes(b"ignored\n")

    with repository.open_worktree(None) as snapshot:
        assert (snapshot.path / "README.md").read_text() == "uncommitted edit\n"
        assert (snapshot.path / "docs" / "new.md").read_text() == "# New\n"
        assert not (snapshot.path / "noise.log").exists()
        assert not snapshot.has_commit()
        assert b"+uncommitted edit" in snapshot.diff(None)


def test_keeps_the_project_untouched_while_a_snapshot_worktree_is_open(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")

    with repository.open_worktree(None) as snapshot:
        location = snapshot.path
        (snapshot.path / "README.md").write_bytes(b"changed in the snapshot\n")

    assert (repository.path / "README.md").read_text() == "# Project\n"
    assert repository.read_status() == ()
    assert not location.exists()


def test_removes_the_worktree_once_it_closes(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")

    with repository.open_worktree() as worktree:
        location = worktree.path

    assert not location.exists()
    assert str(location) not in run_git(repository.path, "worktree", "list", "--porcelain")


def test_clears_worktrees_leaked_by_a_killed_process(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    leaked = tmp_path / "orphan"
    run_git(repository.path, "worktree", "add", "--detach", str(leaked), "HEAD")
    shutil.rmtree(leaked)

    assert str(leaked) in run_git(repository.path, "worktree", "list", "--porcelain")

    with repository.open_worktree():
        assert str(leaked) not in run_git(repository.path, "worktree", "list", "--porcelain")


def test_rejects_initializing_without_a_git_executable(tmp_path: Path) -> None:
    with pytest.raises(git.NotInstalledError):
        git.Repository.init(tmp_path / "project", executable="missing-git-executable")

    assert not (tmp_path / "project").exists()


def test_rejects_initializing_a_repository_over_a_file(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.write_bytes(b"not a directory\n")

    with pytest.raises(git.Error):
        git.Repository.init(target)


def test_reports_no_root_when_git_is_not_installed(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = create_repository(tmp_path / "repo")
    monkeypatch.setenv("PATH", str(tmp_path / "without-git"))

    assert git.find_root(repository.path) is None


@pytest.mark.skipif(sys.platform == "win32", reason="a Git that ends itself needs a shell and `kill`")
def test_refuses_to_place_a_root_a_killed_git_never_answered_for(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = create_repository(tmp_path / "repo")
    nested = repository.path / "packages"
    nested.mkdir()
    (repository.path / ".git" / WINDOW_MARKER).touch()
    install_a_killing_git(monkeypatch, repository.path, ROOT_QUESTION)

    with pytest.raises(git.Error):
        git.find_root(nested)
    with pytest.raises(git.Error):
        git.Repository.init(nested)

    # Read as `no worktree holds this`, the silence puts a second
    # repository inside the one that is already there.
    assert not (nested / ".git").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="a Git that ends itself needs a shell and `kill`")
def test_refuses_to_call_a_killed_git_a_missing_worktree(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / ".git" / WINDOW_MARKER).touch()
    install_a_killing_git(monkeypatch, repository.path, WORKTREE_QUESTION)

    with pytest.raises(git.Error) as raised:
        git.Repository(repository.path)

    assert not isinstance(raised.value, git.NotRepositoryError)


def test_rejects_reading_the_head_of_a_repository_without_commits(tmp_path: Path) -> None:
    repository = git.Repository.init(tmp_path / "project")

    with pytest.raises(git.Error):
        repository.read_head()


def test_reports_which_revisions_name_a_commit(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    first = repository.read_head()
    blob = run_git(repository.path, "rev-parse", "HEAD:README.md")

    assert repository.has_commit(first)
    assert not repository.has_commit("no-such-revision")
    assert not repository.has_commit(blob)


@pytest.mark.skipif(sys.platform == "win32", reason="a Git that ends itself needs a shell and `kill`")
def test_refuses_to_answer_for_a_git_that_never_answered(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = create_repository(tmp_path / "repo")
    # The shim is armed by the marker a commit window leaves, and this
    # asks about a repository that is past every window there is.
    (repository.path / ".git" / WINDOW_MARKER).touch()
    install_a_killing_git(monkeypatch, repository.path, HEAD_QUESTION)

    with pytest.raises(git.Error):
        git.Repository(repository.path).has_commit()

    assert repository.has_commit()


def test_reports_deleted_paths_on_the_side_they_were_deleted_from(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "notes.md").write_bytes(b"notes\n")
    repository.stage(["notes.md"])
    repository.commit("jri: add notes")

    run_git(repository.path, "rm", "-q", "README.md")
    (repository.path / "notes.md").unlink()

    assert {(item.path, item.index, item.worktree) for item in repository.read_status()} == {
        ("README.md", "D", " "),
        ("notes.md", " ", "D"),
    }


def test_rejects_reading_a_file_a_revision_does_not_hold(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")

    with pytest.raises(git.Error):
        repository.read_file("HEAD", "missing.md")

    with pytest.raises(git.Error):
        repository.read_file("no-such-revision", "README.md")


def test_reads_binary_content_as_raw_bytes(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    content = bytes(range(256))
    (repository.path / "logo.bin").write_bytes(content)
    repository.stage(["logo.bin"])

    revision = repository.commit("jri: add a logo")

    assert repository.read_file(revision, "logo.bin") == content


def test_reads_only_the_tree_below_the_requested_path(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "docs").mkdir()
    (repository.path / "docs" / "guide.md").write_bytes(b"# Guide\n")
    repository.stage(["docs"])
    revision = repository.commit("jri: add docs")

    assert repository.read_tree(revision, "docs") == {"docs/guide.md": b"# Guide\n"}
    assert repository.read_tree(revision, "missing") == {}


def test_rejects_a_commit_with_nothing_staged(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")

    with pytest.raises(git.Error):
        repository.commit("jri: test")


# A hunk header counts the lines its body has to hold, so the two are
# weighed against each other and a patch whose body disagrees is
# refused whole. Reading the body over the header instead answers a
# hunk cut back to its context with a patch that applies and writes
# nothing, which is an ending no caller can tell from a patch placed.
@pytest.mark.parametrize("patch", MISCOUNTED_PATCHES, ids=["overcounted", "cut short"])
def test_rejects_a_patch_whose_hunk_counts_are_wrong(
    tmp_path: Path, create_repository: CreateRepository, patch: bytes
) -> None:
    repository = create_repository(tmp_path / "repo")

    with pytest.raises(git.Error):
        repository.apply_patch(patch)

    assert (repository.path / "README.md").read_bytes() == b"# Project\n"


def test_removes_the_worktree_when_the_body_raises(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    locations: list[Path] = []

    # Wrapped so the raising block stays out of `pytest.raises`.
    def fail_inside_the_worktree() -> None:
        with repository.open_worktree() as worktree:
            locations.append(worktree.path)
            raise ZeroDivisionError

    with pytest.raises(ZeroDivisionError):
        fail_inside_the_worktree()

    assert not locations[0].exists()
    assert str(locations[0]) not in run_git(repository.path, "worktree", "list", "--porcelain")


def test_survives_a_worktree_that_was_already_removed(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")

    with repository.open_worktree() as worktree:
        location = worktree.path
        run_git(repository.path, "worktree", "remove", "--force", str(location))

    assert not location.exists()


def test_rejects_opening_a_worktree_at_an_unknown_revision(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = create_repository(tmp_path / "repo")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    with pytest.raises(git.Error), repository.open_worktree("no-such-revision"):
        pass

    assert list(scratch.iterdir()) == []
