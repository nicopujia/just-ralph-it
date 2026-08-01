import shutil
import subprocess
from pathlib import Path

import pytest

from jri.lib import git


def run_git(path: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    assert executable is not None
    return subprocess.run(
        [executable, "-C", str(path), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


def create_repository(path: Path) -> git.Repository:
    path.mkdir()
    run_git(path, "init", "-q")
    run_git(path, "config", "user.name", "Test User")
    run_git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("first\n")
    run_git(path, "add", "README.md")
    run_git(path, "commit", "-qm", "first")
    return git.Repository(path)


def test_rejects_missing_git_and_initializes_repository(tmp_path: Path) -> None:
    with pytest.raises(git.NotInstalledError):
        git.Repository(tmp_path, executable="missing-git-executable")
    repository = git.Repository(tmp_path)

    assert (tmp_path / ".git").is_dir()
    assert not repository.has_head()


def test_finds_worktree_root_from_any_subdirectory(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "repo")
    nested = repository.path / "packages" / "app"
    nested.mkdir(parents=True)

    assert git.find_root(nested) == repository.path
    assert git.find_root(repository.path) == repository.path
    assert git.find_root(tmp_path) is None


def test_inspects_revisions_files_diffs_and_status(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "repo")
    first = repository.read_head()
    (repository.path / "README.md").write_text("second\n")
    (repository.path / "new file.txt").write_text("new\n")

    assert repository.read_file(first, "README.md") == b"first\n"
    assert repository.read_tree(first) == {"README.md": b"first\n"}
    assert repository.read_tracked_paths(first) == ("README.md",)
    assert b"+second" in repository.diff(first, paths=["README.md"])
    assert {(item.path, item.index, item.worktree) for item in repository.read_status()} == {
        ("README.md", " ", "M"),
        ("new file.txt", "?", "?"),
    }

    repository.stage(["README.md", "new file.txt"])
    second = repository.commit("jri: test\n\nCo-authored-by: Test Person <test@example.com>\n")

    assert repository.is_ancestor(first, second)
    assert not repository.is_ancestor(second, first)
    assert repository.read_status() == ()
    assert run_git(repository.path, "show", "-s", "--format=%B", second) == (
        "jri: test\n\nCo-authored-by: Test Person <test@example.com>"
    )


def test_applies_patch(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "repo")
    (repository.path / "README.md").write_text("updated\n")
    patch = repository.diff("HEAD", paths=["README.md"])
    (repository.path / "README.md").write_text("first\n")
    repository.stage(["README.md"])

    repository.apply_patch(patch, index=True)

    assert (repository.path / "README.md").read_text() == "updated\n"
    assert repository.read_status()[0].index == "M"
    with pytest.raises(git.Error):
        repository.apply_patch(patch)


def test_creates_and_removes_worktree(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "repo")

    with repository.open_worktree() as worktree:
        location = worktree.path
        assert location.exists()
        assert worktree.read_head() == repository.read_head()
        assert run_git(location, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"

    assert not location.exists()
    assert str(location) not in run_git(repository.path, "worktree", "list", "--porcelain")


def test_clears_worktrees_leaked_by_a_killed_process(tmp_path: Path) -> None:
    repository = create_repository(tmp_path / "repo")
    leaked = tmp_path / "orphan"
    run_git(repository.path, "worktree", "add", "--detach", str(leaked), "HEAD")
    shutil.rmtree(leaked)

    assert str(leaked) in run_git(repository.path, "worktree", "list", "--porcelain")

    with repository.open_worktree():
        assert str(leaked) not in run_git(repository.path, "worktree", "list", "--porcelain")
