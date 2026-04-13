from pathlib import Path

import pytest

from jri.core.errors import JriError
from jri.core.git import GitRepo
from jri.core.service import JriService
from tests.conftest import run_cli
from tests.helpers import git


def make_git_repo(tmp_path: Path, *, branch: str, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-b", branch)
    git(repo, "config", "user.name", "JRI Tests")
    git(repo, "config", "user.email", "jri-tests@example.com")
    (repo / "README.md").write_text("# temp repo\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    return repo


def test_default_branch_uses_origin_head_when_off_default_branch(
    tmp_path: Path,
) -> None:
    source = make_git_repo(tmp_path, branch="trunk", name="source")
    remote = tmp_path / "remote.git"
    clone = tmp_path / "clone"

    git(source, "clone", "--bare", ".", str(remote))
    git(source, "clone", str(remote), str(clone))
    git(clone, "config", "user.name", "JRI Tests")
    git(clone, "config", "user.email", "jri-tests@example.com")
    git(clone, "checkout", "-b", "feature/x")

    assert GitRepo(clone).default_branch() == "trunk"


def test_default_branch_uses_local_branch_when_repo_is_detached(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path, branch="trunk")

    git(repo, "checkout", "HEAD~0")

    assert GitRepo(repo).default_branch() == "trunk"


def test_default_branch_rejects_ralph(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path, branch="ralph")

    with pytest.raises(
        JriError,
        match=(
            "detected default branch 'ralph'; change the repository default branch name"
        ),
    ):
        GitRepo(repo).default_branch()


def test_handle_wrong_branch_prompts_for_detected_default_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo(tmp_path, branch="trunk")
    assert run_cli(["init"], cwd=repo) == 0
    git(repo, "checkout", "-b", "feature/x")

    service = JriService(repo)
    monkeypatch.setattr("builtins.input", lambda: "y")

    service._handle_wrong_branch(force=False)

    output = capsys.readouterr().out
    assert 'Currently on branch "feature/x", expected "trunk".' in output
    assert "Switch to trunk? [Y/n]" in output
    assert git(repo, "branch", "--show-current") == "trunk"
