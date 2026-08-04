from pathlib import Path

from jri.core.repository import CO_AUTHOR, Repository
from tests.conftest import CreateRepository, RunGit


def test_credits_ralph_without_being_asked(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    create_repository(tmp_path)
    repository = Repository(tmp_path)
    (tmp_path / "README.md").write_text("second\n")
    repository.stage(["README.md"])

    commit = repository.commit("jri: test")

    assert run_git(tmp_path, "show", "-s", "--format=%(trailers:key=Co-authored-by,valueonly)", commit) == CO_AUTHOR


def test_credits_ralph_from_a_worktree_too(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    create_repository(tmp_path)
    repository = Repository(tmp_path)

    with repository.open_worktree() as worktree:
        assert isinstance(worktree, Repository)
        (worktree.path / "README.md").write_text("second\n")
        worktree.stage(["README.md"])
        commit = worktree.commit("jri: test")
        trailer = run_git(worktree.path, "show", "-s", "--format=%(trailers:key=Co-authored-by,valueonly)", commit)

    assert trailer == CO_AUTHOR
