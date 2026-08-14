import os
import shutil
import sys
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
    STAGING_QUESTION,
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
# `init` writes config, then HEAD, under separate locks. A kill between the two writes can leave either lock
# standing while only the other file is still missing.
KILLED_INITS = (("config.lock", ("config", "HEAD")), ("HEAD.lock", ("HEAD",)))
# Git ignores SIGPIPE so a broken output pipe does not end it. Sending it here would not kill Git, so it is left
# out of the signals this test can use to end one.
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


def test_initializes_a_nested_repository_of_its_own(tmp_path: Path, create_repository: CreateRepository) -> None:
    enclosing = create_repository(tmp_path / "repo")
    nested = tmp_path / "repo" / "packages"
    nested.mkdir()

    repository = git.Repository.init(nested, nested=True)

    assert repository.path == nested.resolve()
    assert not repository.has_commit()
    # The enclosing repository reads the nested repository as one untracked directory, and its index never
    # receives what that repository holds.
    assert enclosing.read_status() == (git.Status("packages/", "?", "?"),)


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
    (repository.path / "notes.md").write_bytes(b"# Notes\n")
    repository.stage(["notes.md"])
    revision = repository.commit("docs: add notes")
    (repository.path / "README.md").write_bytes(b"second\n")
    (repository.path / "notes.md").write_bytes(b"# Renamed notes\n")

    patch = repository.diff(revision, paths=["README.md"])

    assert b"+second" in patch
    assert b"notes.md" not in patch


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
        # A standing lock must still block a fresh command attempt, not just be left behind after the first one.
        with pytest.raises(git.Error, match=r"HEAD\.lock"):
            repository.commit("second", paths=("README.md",))
    finally:
        end_the_second_command(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="a hook that signals its own Git needs a shell and `kill`")
@pytest.mark.parametrize("name", HANDLED_SIGNALS_A_COMMIT_DIES_OF)
def test_keeps_the_index_lock_another_command_took_when_a_signal_ends_its_own_git(
    tmp_path: Path, create_repository: CreateRepository, name: str
) -> None:
    repository = create_repository(tmp_path)
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")
    # `past` stands after the commit gave the index lock back, so the lock that the second command takes there is
    # its own. Git removes its own locks at a signal it handles, thus what stands after belongs to that command.
    window = HOLD_THE_LOCK.format(directory=tmp_path / ".git", lock="index.lock") + SIGNAL_THE_GIT.format(name=name)

    with open_a_window(tmp_path, "past", window), pytest.raises(git.Error):
        repository.commit("second", paths=("README.md",))

    try:
        assert is_the_second_command_running(tmp_path)
        assert read_git_locks(tmp_path) == (tmp_path / ".git/index.lock",)
    finally:
        end_the_second_command(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="a file that refuses a read is an access list `chmod` cannot write")
def test_reports_git_refusing_a_repository_whose_head_cannot_be_read(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")
    # Git reports an unreadable HEAD with this exact message, not a permission error.
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
    window = HOLD_THE_LOCK.format(directory=tmp_path / ".git", lock=lock) + KILL_THE_GIT

    with (
        open_a_window(tmp_path, "worktree", window),
        pytest.raises(git.Error),
        repository.open_worktree("HEAD", location=tmp_path / "checkout"),
    ):
        pass

    try:
        assert is_the_second_command_running(tmp_path)
        assert read_git_locks(tmp_path) == (tmp_path / ".git" / lock,)
    finally:
        end_the_second_command(tmp_path)


def test_reports_the_locks_over_the_files_a_command_of_its_own_writes(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path)
    branch = tmp_path / ".git" / f"{run_git(tmp_path, 'symbolic-ref', 'HEAD')}.lock"
    # A commit moves HEAD and the branch it stands on, and each write of the index locks the index. A lock over
    # `config` belongs to no command of JRI, so it must stay out of the report.
    for lock in (tmp_path / ".git/index.lock", tmp_path / ".git/HEAD.lock", branch, tmp_path / ".git/config.lock"):
        lock.touch()

    assert repository.locks.blocking == (tmp_path / ".git/index.lock", tmp_path / ".git/HEAD.lock", branch)


# Git answers with the common directory as a path from the directory the command ran in. A repository opened at a
# subdirectory must join that answer onto that same directory. Joined onto the top level, it points above the
# repository, and the branch lock that stops a commit stands at a path nothing looks at.
def test_reports_the_locks_of_a_repository_it_opened_at_a_subdirectory(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    create_repository(tmp_path)
    nested = tmp_path / "packages" / "app"
    nested.mkdir(parents=True)
    branch = tmp_path / ".git" / f"{run_git(tmp_path, 'symbolic-ref', 'HEAD')}.lock"
    branch.touch()

    assert git.Repository(nested).locks.blocking == (branch,)


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
def test_frees_the_locks_the_git_it_started_died_holding(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path)
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")

    with open_a_window(tmp_path, "index", KILL_THE_GIT), pytest.raises(git.Error):
        repository.commit("second", paths=("README.md",))

    assert read_git_locks(tmp_path) == ()
    # Freeing the lock must not corrupt the repository: the killed commit must not have partly landed,
    # and a new commit must still succeed.
    assert run_git(tmp_path, "log", "--format=%s") == "initial"
    assert repository.commit("second", paths=("README.md",))
    assert run_git(tmp_path, "log", "--format=%s").splitlines() == ["second", "initial"]


@pytest.mark.skipif(sys.platform == "win32", reason="a hook that kills its own Git needs a shell and `kill`")
def test_leaves_the_refs_a_commit_died_inside_its_transaction_holding(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path)
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")
    branch = tmp_path / ".git" / f"{run_git(tmp_path, 'symbolic-ref', 'HEAD')}.lock"

    with open_a_window(tmp_path, "branch", KILL_THE_GIT), pytest.raises(git.Error):
        repository.commit("second", paths=("README.md",))

    # Git moves HEAD and the branch ref inside one transaction. A stopped process cannot show whether that
    # transaction is still live elsewhere, so JRI must leave both locks for a person to resolve.
    assert read_git_locks(tmp_path) == (tmp_path / ".git/HEAD.lock", branch)
    assert run_git(tmp_path, "log", "--format=%s") == "initial"
    with pytest.raises(git.Error, match=r"HEAD\.lock|cannot lock ref"):
        repository.commit("second", paths=("README.md",))


@pytest.mark.skipif(sys.platform == "win32", reason="a hook that kills its own Git needs a shell and `kill`")
@pytest.mark.parametrize("lock", ["HEAD.lock", "branch"])
def test_keeps_the_ref_lock_a_running_command_holds_when_a_kill_ends_a_commit(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit, lock: str
) -> None:
    repository = create_repository(tmp_path)
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")
    name = f"{run_git(tmp_path, 'symbolic-ref', 'HEAD')}.lock" if lock == "branch" else lock
    window = HOLD_THE_LOCK.format(directory=tmp_path / ".git", lock=name) + KILL_THE_GIT

    with open_a_window(tmp_path, "index", window), pytest.raises(git.Error):
        repository.commit("second", paths=("README.md",))

    try:
        assert is_the_second_command_running(tmp_path)
        assert read_git_locks(tmp_path) == (tmp_path / ".git" / name,)
    finally:
        end_the_second_command(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="a Git that ends itself needs a shell and `kill`")
def test_keeps_the_index_lock_that_was_standing_before_a_staging_a_kill_ended(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_repository(tmp_path)
    index_lock = tmp_path / ".git/index.lock"
    index_lock.touch()
    (tmp_path / ".git" / WINDOW_MARKER).touch()
    install_a_killing_git(monkeypatch, tmp_path, STAGING_QUESTION)
    repository = git.Repository(tmp_path)

    with pytest.raises(git.Error):
        repository.stage(("README.md",))

    assert read_git_locks(tmp_path) == (index_lock,)


@pytest.mark.skipif(sys.platform == "win32", reason="a filter that kills its own Git needs a shell and `kill`")
def test_frees_the_index_lock_the_apply_it_started_died_holding(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)

    with open_a_filter_window(tmp_path, RECORD_THE_LOCKS + KILL_THE_GIT, side="smudge"), pytest.raises(git.Error):
        repository.apply_patch(RENAMING_PATCH, index=True)

    assert read_the_locks_the_window_saw(tmp_path) == (".git/index.lock",)
    assert read_git_locks(tmp_path) == ()
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")
    assert repository.commit("second", paths=("README.md",))


@pytest.mark.skipif(sys.platform == "win32", reason="a filter that kills its own Git needs a shell and `kill`")
def test_keeps_the_lock_a_running_command_holds_when_a_kill_ends_an_apply_that_took_none(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
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

    assert read_the_locks_the_window_saw(tmp_path) == (".git/index.lock",)
    assert read_git_locks(tmp_path) == ()
    assert repository.commit("second", paths=("README.md",))


@pytest.mark.skipif(sys.platform == "win32", reason="a filter that kills its own Git needs a shell and `kill`")
def test_frees_the_index_lock_the_restore_it_started_died_holding(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    # Git runs the smudge side over what it is about to write back into the worktree, and only for a path whose
    # bytes it must replace. The edit below makes README.md that path.
    (tmp_path / "README.md").write_bytes(b"# Edited\n")

    with open_a_filter_window(tmp_path, RECORD_THE_LOCKS + KILL_THE_GIT, side="smudge"), pytest.raises(git.Error):
        repository.restore("HEAD", ["README.md"])

    assert read_the_locks_the_window_saw(tmp_path) == (".git/index.lock",)
    assert read_git_locks(tmp_path) == ()
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")
    assert repository.commit("second", paths=("README.md",))


@pytest.mark.skipif(sys.platform == "win32", reason="a filter that kills its own Git needs a shell and `kill`")
def test_frees_the_index_lock_the_unstaging_it_started_died_holding(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    # An unstaging refreshes the index while it holds the index lock. Only a file that the index cannot date
    # makes that refresh read the worktree back, which is where the kill below must land.
    stale_the_filtered_path(tmp_path)

    with open_a_filter_window(tmp_path, RECORD_THE_LOCKS + KILL_THE_GIT, side="clean"), pytest.raises(git.Error):
        repository.unstage(["README.md"])

    assert read_the_locks_the_window_saw(tmp_path) == (".git/index.lock",)
    assert read_git_locks(tmp_path) == ()
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")
    assert repository.commit("second", paths=("README.md",))


@pytest.mark.skipif(sys.platform == "win32", reason="a filter that kills its own Git needs a shell and `kill`")
def test_keeps_the_lock_over_head_a_running_command_holds_when_a_kill_ends_a_staging(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path)
    (tmp_path / "README.md").write_bytes(b"# Project\nTotals are supported.\n")
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
    # Make Git distrust the cached stat for the file. Only then does `read_status` re-run the clean filter,
    # which is where the kill below must land.
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
    # A commit with no named paths never adds `--`, so JRI does not count itself as the index-lock owner.
    # Getting this wrong would delete another process's real lock instead of leaving it standing.
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
    # Set an old mtime so Git cannot trust its cached stat and wants to refresh the index. This proves
    # `--no-optional-locks` stops that opportunistic write, not just an index that was already clean.
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
    (repository.path / "README.md").write_bytes(b"fourth\n")
    repository.stage(["README.md"])
    newest = repository.commit("jri: test again", ["JRI-Test: accepted"])

    assert repository.find_commit("JRI-Test: accepted") == newest
    assert repository.find_commit("JRI-Test: accepted", "HEAD~2") == marked
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


def test_reads_the_paths_the_index_holds_as_links(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "notes.md").write_bytes(b"README.md")
    blob = run_git(repository.path, "hash-object", "-w", "--", "notes.md")
    # Fake a symlink-mode index entry instead of creating a real symlink, which needs privilege that not every
    # system, such as Windows without it, grants.
    run_git(repository.path, "update-index", "--add", "--cacheinfo", f"120000,{blob},notes.md")

    assert repository.read_staged_paths(linked=True) == ("notes.md",)
    assert repository.read_staged_paths(["README.md"], linked=True) == ()
    assert not (repository.path / "notes.md").is_symlink()


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


def test_reports_a_rename_the_worktree_alone_holds_with_its_original_path(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "old.md").write_bytes(b"# Old\nThe store keeps orders.\n")
    repository.stage(["old.md"])
    repository.commit("docs: add old")
    (repository.path / "old.md").rename(repository.path / "renamed.md")
    (repository.path / "other.md").write_bytes(b"# Other\nThe reporter renders totals.\n")

    repository.stage(["other.md", "renamed.md"], intent_to_add=True)

    # Git reports the rename in the worktree column, and puts the original path in a record of its own. A reader
    # that misses that record reads it as the status of another path.
    assert repository.read_status() == (git.Status("other.md", " ", "A"), git.Status("renamed.md", " ", "R", "old.md"))


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

    with repository.open_worktree(location=tmp_path / "checkout") as worktree:
        assert worktree.path == (tmp_path / "checkout").resolve()
        assert worktree.read_head() == repository.read_head()
        assert run_git(worktree.path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def test_opens_two_worktrees_at_once_at_two_locations(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")

    with (
        repository.open_worktree(location=tmp_path / "checkout") as checkout,
        repository.open_worktree(None, location=tmp_path / "snapshot") as snapshot,
    ):
        locations = (checkout.path, snapshot.path)

        assert checkout.path == (tmp_path / "checkout").resolve()
        assert snapshot.path == (tmp_path / "snapshot").resolve()
        assert (checkout.path / "README.md").read_bytes() == b"# Project\n"

    assert not any(location.exists() for location in locations)


def test_snapshots_the_working_tree_when_no_revision_is_given(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_bytes(b"uncommitted edit\n")
    (repository.path / "docs").mkdir()
    (repository.path / "docs" / "new.md").write_bytes(b"# New\n")
    (repository.path / ".gitignore").write_bytes(b"*.log\n")
    (repository.path / "noise.log").write_bytes(b"ignored\n")

    with repository.open_worktree(None, location=tmp_path / "snapshot") as snapshot:
        assert (snapshot.path / "README.md").read_text() == "uncommitted edit\n"
        assert (snapshot.path / "docs" / "new.md").read_text() == "# New\n"
        assert not (snapshot.path / "noise.log").exists()
        assert not snapshot.has_commit()
        assert b"+uncommitted edit" in snapshot.diff(None)


def test_keeps_the_project_untouched_while_a_snapshot_worktree_is_open(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")

    with repository.open_worktree(None, location=tmp_path / "snapshot") as snapshot:
        location = snapshot.path
        (snapshot.path / "README.md").write_bytes(b"changed in the snapshot\n")

    assert (repository.path / "README.md").read_text() == "# Project\n"
    assert repository.read_status() == ()
    assert not location.exists()


def test_removes_the_worktree_once_it_closes(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")

    with repository.open_worktree(location=tmp_path / "checkout") as worktree:
        location = worktree.path

    assert not location.exists()
    assert location.as_posix() not in run_git(repository.path, "worktree", "list", "--porcelain")


def test_clears_worktrees_leaked_by_a_killed_process(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    leaked = tmp_path / "orphan"
    run_git(repository.path, "worktree", "add", "--detach", str(leaked), "HEAD")
    shutil.rmtree(leaked)

    assert leaked.as_posix() in run_git(repository.path, "worktree", "list", "--porcelain")

    with repository.open_worktree(location=tmp_path / "checkout"):
        assert leaked.as_posix() not in run_git(repository.path, "worktree", "list", "--porcelain")


# A killed process leaves its worktree, and the process after it asks for that same location. Git refuses a
# location that holds files, and it refuses one that an entry of its own still names.
def test_opens_a_worktree_where_a_killed_process_left_one(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    location = tmp_path / "checkout"
    run_git(repository.path, "worktree", "add", "--detach", str(location), "HEAD")
    (location / "left-behind.md").write_bytes(b"what the killed process was reading\n")

    with repository.open_worktree(location=location) as worktree:
        assert worktree.read_head() == repository.read_head()
        assert not (worktree.path / "left-behind.md").exists()

    assert not location.exists()


def test_copies_the_worktree_where_a_killed_process_left_one(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")
    location = tmp_path / "snapshot"
    location.mkdir()
    (location / "left-behind.md").write_bytes(b"what the killed process was reading\n")

    with repository.open_worktree(None, location=location) as snapshot:
        assert (snapshot.path / "README.md").read_bytes() == b"# Project\n"
        assert not (snapshot.path / "left-behind.md").exists()

    assert not location.exists()


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

    def fail_inside_the_worktree() -> None:
        with repository.open_worktree(location=tmp_path / "checkout") as worktree:
            locations.append(worktree.path)
            raise ZeroDivisionError

    with pytest.raises(ZeroDivisionError):
        fail_inside_the_worktree()

    assert not locations[0].exists()
    assert locations[0].as_posix() not in run_git(repository.path, "worktree", "list", "--porcelain")


def test_survives_a_worktree_that_was_already_removed(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")

    with repository.open_worktree(location=tmp_path / "checkout") as worktree:
        location = worktree.path
        run_git(repository.path, "worktree", "remove", "--force", str(location))

    assert not location.exists()


def test_rejects_opening_a_worktree_at_an_unknown_revision(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    location = tmp_path / "checkout"

    with pytest.raises(git.Error), repository.open_worktree("no-such-revision", location=location):
        pass

    assert not location.exists()
