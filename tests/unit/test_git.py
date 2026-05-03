import subprocess
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


def test_default_branch_allows_ralph(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path, branch="ralph")

    assert GitRepo(repo).default_branch() == "ralph"


def test_ralph_branch_uses_detected_default_branch(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path, branch="trunk")

    assert GitRepo(repo).ralph_branch() == "ralph/trunk"


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


def completed(
    *args: str,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git", *args], returncode, stdout, stderr)


def test_default_branch_uses_only_local_branch_when_no_head_points_to_it(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path, branch="trunk")
    git(repo, "checkout", "--detach")

    assert GitRepo(repo).default_branch() == "trunk"


def test_default_branch_falls_back_to_main_when_branch_cannot_be_inferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        _ = check
        if args == ("branch", "--show-current"):
            return completed(*args, stdout="\n")
        return completed(*args, returncode=1)

    monkeypatch.setattr(repo, "run", fake_run)

    assert repo.default_branch() == "main"


@pytest.mark.parametrize(
    "branch",
    [
        "",
        "-main",
        "feature branch",
        "feature..branch",
        "feature//branch",
        "feature@{1}",
        "feature.lock",
    ],
)
def test_default_branch_name_validation_rejects_unsafe_names(
    tmp_path: Path, branch: str
) -> None:
    repo = GitRepo(tmp_path)

    with pytest.raises(JriError, match="invalid default branch name"):
        repo.validate_default_branch_name(branch)


def test_checkout_new_branch_failure_uses_git_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fail_create_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=128, stderr="bad ref\n")

    monkeypatch.setattr(repo, "run", fail_create_run)

    with pytest.raises(JriError, match="bad ref"):
        repo.checkout_new_branch("topic")


def test_checkout_failure_uses_fallback_message_when_git_has_no_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fail_checkout_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=1)

    monkeypatch.setattr(repo, "run", fail_checkout_run)

    with pytest.raises(JriError, match="failed to checkout topic"):
        repo.checkout("topic")


def test_ensure_default_branch_rejects_when_current_branch_differs(
    tmp_path: Path,
) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")
    git(repo_path, "checkout", "-b", "feature")

    with pytest.raises(JriError, match="jri start must begin from the main branch"):
        GitRepo(repo_path).ensure_default_branch(hint="main")


def test_ensure_local_branch_rejects_missing_branch(tmp_path: Path) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")

    with pytest.raises(JriError, match="default branch 'trunk' does not exist"):
        GitRepo(repo_path).ensure_local_branch("trunk")


def test_commit_failure_uses_git_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fail_commit_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=1, stderr="nothing to commit\n")

    monkeypatch.setattr(repo, "run", fail_commit_run)

    with pytest.raises(JriError, match="nothing to commit"):
        repo.commit("save work")


def test_commit_failure_uses_fallback_message_when_git_has_no_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fail_commit_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=1)

    monkeypatch.setattr(repo, "run", fail_commit_run)

    with pytest.raises(JriError, match="failed to commit: save work"):
        repo.commit("save work")


def test_commit_all_if_needed_skips_clean_tree(tmp_path: Path) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")

    assert GitRepo(repo_path).commit_all_if_needed("save work") is False


def test_commit_all_if_needed_commits_dirty_tree(tmp_path: Path) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")
    (repo_path / "README.md").write_text("# changed\n", encoding="utf-8")

    assert GitRepo(repo_path).commit_all_if_needed("save work") is True
    assert git(repo_path, "log", "-1", "--pretty=%s") == "save work"
    assert GitRepo(repo_path).status_short() == ""
