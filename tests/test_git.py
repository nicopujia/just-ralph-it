import shutil
import tempfile
from pathlib import Path

import pytest

from jri.lib import git
from tests.conftest import CreateRepository, RunGit


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
    (repository.path / "README.md").write_text("second\n")

    assert repository.read_file(revision, "README.md") == b"# Project\n"
    assert repository.read_tree(revision) == {"README.md": b"# Project\n"}
    assert repository.read_tracked_paths(revision) == ("README.md",)


def test_diffs_the_worktree_against_a_revision(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    revision = repository.read_head()
    (repository.path / "README.md").write_text("second\n")

    assert b"+second" in repository.diff(revision, paths=["README.md"])


def test_reports_changed_and_untracked_paths(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_text("second\n")
    (repository.path / "new file.txt").write_text("new\n")

    assert {(item.path, item.index, item.worktree) for item in repository.read_status()} == {
        ("README.md", " ", "M"),
        ("new file.txt", "?", "?"),
    }


def test_moves_staged_paths_to_the_index_side_of_the_status(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_text("second\n")
    (repository.path / "new file.txt").write_text("new\n")

    repository.stage(["README.md", "new file.txt"])

    assert {(item.path, item.index, item.worktree) for item in repository.read_status()} == {
        ("README.md", "M", " "),
        ("new file.txt", "A", " "),
    }


def test_commits_staged_paths_with_a_co_author(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_text("second\n")
    repository.stage(["README.md"])

    commit = repository.commit("jri: test", "Test Person <test@example.com>")

    assert run_git(repository.path, "show", "-s", "--format=%B", commit) == (
        "jri: test\n\nCo-authored-by: Test Person <test@example.com>"
    )


def test_commits_staged_paths_without_a_co_author(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_text("second\n")
    repository.stage(["README.md"])

    commit = repository.commit("jri: test")

    assert run_git(repository.path, "show", "-s", "--format=%B", commit) == "jri: test"


def test_reports_which_revision_descends_from_which(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    first = repository.read_head()
    (repository.path / "README.md").write_text("second\n")
    repository.stage(["README.md"])
    second = repository.commit("jri: test", "Test Person <test@example.com>")

    assert repository.is_ancestor(first, second)
    assert not repository.is_ancestor(second, first)


def test_reports_renames_with_their_original_path(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    run_git(repository.path, "mv", "README.md", "docs.md")
    (repository.path / "untracked.md").write_text("new\n")

    status = repository.read_status()

    assert [(item.path, item.index, item.original_path) for item in status] == [
        ("docs.md", "R", "README.md"),
        ("untracked.md", "?", None),
    ]


def test_applies_a_patch_to_the_worktree_and_the_index(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_text("updated\n")
    patch = repository.diff("HEAD", paths=["README.md"])
    (repository.path / "README.md").write_text("# Project\n")
    repository.stage(["README.md"])

    repository.apply_patch(patch, index=True)

    assert (repository.path / "README.md").read_text() == "updated\n"
    assert repository.read_status()[0].index == "M"


def test_rejects_a_patch_that_does_not_apply(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_text("updated\n")
    patch = repository.diff("HEAD", paths=["README.md"])

    with pytest.raises(git.Error):
        repository.apply_patch(patch)


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
    (repository.path / "README.md").write_text("uncommitted edit\n")
    (repository.path / "docs").mkdir()
    (repository.path / "docs" / "new.md").write_text("# New\n")
    (repository.path / ".gitignore").write_text("*.log\n")
    (repository.path / "noise.log").write_text("ignored\n")

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
        (snapshot.path / "README.md").write_text("changed in the snapshot\n")

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


@pytest.mark.xfail(
    strict=True, reason="Repository.init leaks FileExistsError instead of a git.Error when the path is a regular file"
)
def test_rejects_initializing_a_repository_over_a_file(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.write_text("not a directory\n")

    with pytest.raises(git.Error):
        git.Repository.init(target)


def test_reports_no_root_when_git_is_not_installed(
    tmp_path: Path, create_repository: CreateRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = create_repository(tmp_path / "repo")
    monkeypatch.setenv("PATH", str(tmp_path / "without-git"))

    assert git.find_root(repository.path) is None


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


def test_rejects_comparing_against_a_revision_that_does_not_exist(
    tmp_path: Path, create_repository: CreateRepository
) -> None:
    repository = create_repository(tmp_path / "repo")

    with pytest.raises(git.Error):
        repository.is_ancestor("no-such-revision")


def test_reports_deleted_paths_on_the_side_they_were_deleted_from(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "notes.md").write_text("notes\n")
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
    (repository.path / "docs" / "guide.md").write_text("# Guide\n")
    repository.stage(["docs"])
    revision = repository.commit("jri: add docs")

    assert repository.read_tree(revision, "docs") == {"docs/guide.md": b"# Guide\n"}
    assert repository.read_tree(revision, "missing") == {}


def test_rejects_a_commit_with_nothing_staged(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")

    with pytest.raises(git.Error):
        repository.commit("jri: test")


def test_applies_a_patch_whose_hunk_counts_are_wrong(tmp_path: Path, create_repository: CreateRepository) -> None:
    repository = create_repository(tmp_path / "repo")
    patch = b"""\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,7 +1,9 @@
-# Project
+# Renamed
"""

    repository.apply_patch(patch)

    assert (repository.path / "README.md").read_text() == "# Renamed\n"


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
