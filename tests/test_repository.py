from pathlib import Path

from jri.core.repository import Repository
from tests.conftest import CreateRepository, RunGit

# Every commit that JRI makes must give this credit. The test writes the credit out, so a change to the
# shipped name or address makes this test fail.
CO_AUTHOR = "ralphpujia <ralph@pujia.ar>"


def test_credits_ralph_without_being_asked(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    create_repository(tmp_path)
    repository = Repository(tmp_path)
    (tmp_path / "README.md").write_text("second\n")
    repository.stage(["README.md"])

    commit = repository.commit("jri: test")

    assert run_git(tmp_path, "show", "-s", "--format=%(trailers:key=Co-authored-by,valueonly)", commit) == CO_AUTHOR


def test_credits_ralph_beside_the_trailers_it_is_given(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    create_repository(tmp_path)
    repository = Repository(tmp_path)
    (tmp_path / "README.md").write_text("second\n")
    repository.stage(["README.md"])

    commit = repository.commit("jri: test", ["JRI-Test: accepted"])

    assert run_git(tmp_path, "show", "-s", "--format=%B", commit) == (
        f"jri: test\n\nCo-authored-by: {CO_AUTHOR}\nJRI-Test: accepted"
    )


def test_credits_ralph_from_a_worktree_too(
    tmp_path: Path, create_repository: CreateRepository, run_git: RunGit
) -> None:
    create_repository(tmp_path)
    repository = Repository(tmp_path)

    with repository.open_worktree(location=tmp_path / "worktree") as worktree:
        (worktree.path / "README.md").write_text("second\n")
        worktree.stage(["README.md"])
        commit = worktree.commit("jri: test")
        trailer = run_git(worktree.path, "show", "-s", "--format=%(trailers:key=Co-authored-by,valueonly)", commit)

    assert trailer == CO_AUTHOR


def test_hides_an_excluded_path_from_this_clone_alone(tmp_path: Path) -> None:
    repository = Repository.init(tmp_path)
    exclude_file = tmp_path / ".git" / "info" / "exclude"
    exclude_file.write_text("# A rule the user wrote", encoding="utf-8")
    (tmp_path / ".env").write_text("KEY=1\n", encoding="utf-8")

    repository.exclude("/.env")
    repository.exclude("/.env")

    assert not repository.read_status()
    assert exclude_file.read_text(encoding="utf-8") == "# A rule the user wrote\n/.env\n"


def test_excludes_a_path_where_git_holds_no_exclude_file(tmp_path: Path) -> None:
    repository = Repository.init(tmp_path)
    (tmp_path / ".git" / "info" / "exclude").unlink()
    (tmp_path / ".git" / "info").rmdir()
    (tmp_path / ".env").write_text("KEY=1\n", encoding="utf-8")

    repository.exclude("/.env")

    assert not repository.read_status()
