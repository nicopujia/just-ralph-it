import subprocess
from pathlib import Path

import pytest

from jri.core.errors import JriError
from jri.core.git import GitRepo, WorktreeInfo, parse_tag_name, tag_name
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


def test_ralph_branch_falls_back_when_legacy_ralph_branch_exists(
    tmp_path: Path,
) -> None:
    repo = make_git_repo(tmp_path, branch="main")
    git(repo, "checkout", "-b", "ralph")
    git(repo, "checkout", "main")

    assert GitRepo(repo).ralph_branch() == "ralph-main"


def test_host_branch_returns_checked_out_raw_branch(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path, branch="main")
    git(repo, "checkout", "-b", "feature/slash-name")

    assert GitRepo(repo).host_branch() == "feature/slash-name"


def test_host_branch_rejects_detached_head(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path, branch="main")
    git(repo, "checkout", "--detach")

    with pytest.raises(JriError, match="checked-out branch"):
        GitRepo(repo).host_branch()


def test_ralph_branch_for_preserves_raw_host_branch(tmp_path: Path) -> None:
    repo = GitRepo(tmp_path)

    assert repo.ralph_branch_for("feature/slash-name") == "ralph/feature/slash-name"


def test_worktree_list_parser_keeps_path_and_branch_ref(tmp_path: Path) -> None:
    output = (
        f"worktree {tmp_path / 'repo'}\n"
        "HEAD abc123\n"
        "branch refs/heads/main\n"
        "\n"
        f"worktree {tmp_path / 'detached'}\n"
        "HEAD def456\n"
        "detached\n"
        "\n"
    )

    assert GitRepo.parse_worktree_list(output) == (
        WorktreeInfo(tmp_path / "repo", "refs/heads/main"),
        WorktreeInfo(tmp_path / "detached", None),
    )


def test_worktree_list_runs_porcelain_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        _ = check
        if args == ("worktree", "list", "--porcelain"):
            return completed(
                *args,
                stdout=f"worktree {tmp_path}\nHEAD abc123\nbranch refs/heads/main\n",
            )
        raise AssertionError(args)

    monkeypatch.setattr(repo, "run", fake_run)

    assert repo.worktree_list() == (WorktreeInfo(tmp_path, "refs/heads/main"),)


def test_worktree_list_checked_out_branch_conflict_rejects_other_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path / "repo")
    other = tmp_path / "other"
    monkeypatch.setattr(
        repo,
        "worktree_list",
        lambda: (
            WorktreeInfo(repo.root, "refs/heads/main"),
            WorktreeInfo(other, "refs/heads/ralph/main"),
        ),
    )

    with pytest.raises(JriError, match="ralph/main.*already checked out"):
        repo.ensure_branches_not_checked_out_elsewhere("main", "ralph/main")


@pytest.mark.parametrize(
    ("existing_ref", "desired_branch"),
    [
        ("refs/heads/ralph/feature", "ralph/feature/x"),
        ("refs/heads/ralph/feature/x", "ralph/feature"),
    ],
)
def test_ref_namespace_collision_detects_prefix_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_ref: str,
    desired_branch: str,
) -> None:
    repo = GitRepo(tmp_path)

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        _ = check
        if args == ("for-each-ref", "--format=%(refname)", "refs/heads"):
            return completed(*args, stdout=f"{existing_ref}\n")
        raise AssertionError(args)

    monkeypatch.setattr(repo, "run", fake_run)

    with pytest.raises(JriError, match="conflicts with existing ref"):
        repo.ensure_no_local_branch_ref_namespace_collision(desired_branch)


def test_ref_namespace_collision_ignores_exact_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        _ = check
        if args == ("for-each-ref", "--format=%(refname)", "refs/heads"):
            return completed(*args, stdout="refs/heads/ralph/feature\n")
        raise AssertionError(args)

    monkeypatch.setattr(repo, "run", fake_run)

    repo.ensure_no_local_branch_ref_namespace_collision("ralph/feature")


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
    capsys.readouterr()

    with pytest.raises(JriError, match="runtime branch changed"):
        service._handle_wrong_branch(host_branch="trunk")

    assert capsys.readouterr().out == ""
    assert git(repo, "branch", "--show-current") == "feature/x"


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


def test_default_branch_ignores_empty_origin_head_before_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        _ = check
        if args == ("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"):
            return completed(*args, stdout="origin/\n")
        if args == ("rev-parse", "--verify", "--quiet", "refs/heads/main"):
            return completed(*args, returncode=0)
        raise AssertionError(args)

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


def test_ensure_default_branch_allows_current_branch(tmp_path: Path) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")

    GitRepo(repo_path).ensure_default_branch(hint="main")


def test_ensure_local_branch_rejects_missing_branch(tmp_path: Path) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")

    with pytest.raises(JriError, match="default branch 'trunk' does not exist"):
        GitRepo(repo_path).ensure_local_branch("trunk")


def test_ensure_local_branch_accepts_existing_branch(tmp_path: Path) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")

    GitRepo(repo_path).ensure_local_branch("main")


def test_delete_branch_invokes_git_branch_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake_delete_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        calls.append(args)
        return completed(*args)

    monkeypatch.setattr(repo, "run", fake_delete_run)

    repo.delete_branch("topic")

    assert calls == [("branch", "-D", "topic")]


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


def test_tag_name_and_parse_tag_name_round_trip() -> None:
    tag = "jri/begin/task-123"

    assert tag_name("task-123", "begin") == tag
    assert parse_tag_name(tag) == ("begin", "task-123")


@pytest.mark.parametrize(
    "tag",
    ["jri/begin", "jri/begin/task-123/extra", "jri/review/task-123"],
)
def test_parse_tag_name_rejects_invalid_shapes(tag: str) -> None:
    assert parse_tag_name(tag) is None


def test_parse_tag_name_rejects_non_jri_prefix() -> None:
    assert parse_tag_name("other/begin/task-123") is None


def test_init_failure_uses_git_stderr_and_creates_root_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "repo"
    repo = GitRepo(repo_path)

    def fail_init_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=1, stderr="bad init\n")

    monkeypatch.setattr(repo, "run", fail_init_run)

    with pytest.raises(JriError, match="bad init"):
        repo.init()

    assert repo_path.exists()


def test_init_if_needed_skips_existing_repo(tmp_path: Path) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")

    GitRepo(repo_path).init_if_needed()

    assert git(repo_path, "branch", "--show-current") == "main"


def test_init_if_needed_initializes_missing_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = tmp_path / "repo"
    repo = GitRepo(repo_path)

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        _ = check
        if args == ("rev-parse", "--is-inside-work-tree"):
            return completed(*args, returncode=1)
        if args == ("init", "-b", "main"):
            return completed(*args)
        raise AssertionError(args)

    monkeypatch.setattr(repo, "run", fake_run)

    repo.init_if_needed()

    assert repo_path.exists()


def test_relative_path_preserves_relative_input(tmp_path: Path) -> None:
    repo = GitRepo(tmp_path / "repo")

    assert repo.relative_path(Path("dir/file.txt")) == "dir/file.txt"


def test_relative_path_handles_paths_inside_and_outside_root(tmp_path: Path) -> None:
    repo = GitRepo(tmp_path / "repo")
    inside = repo.root / "dir" / "file.txt"
    outside = tmp_path / "outside.txt"

    assert repo.relative_path(inside) == "dir/file.txt"
    assert repo.relative_path(outside) == outside.as_posix()


def test_ensure_repo_rejects_non_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fail_is_repo_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=1)

    monkeypatch.setattr(repo, "run", fail_is_repo_run)

    with pytest.raises(JriError, match="jri requires a git repository"):
        repo.ensure_repo()


def test_ensure_repo_accepts_repo(tmp_path: Path) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")

    GitRepo(repo_path).ensure_repo()


def test_ensure_clean_rejects_dirty_tree(tmp_path: Path) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")
    (repo_path / "README.md").write_text("# changed\n", encoding="utf-8")

    with pytest.raises(JriError, match="git working tree must be clean"):
        GitRepo(repo_path).ensure_clean()


def test_ensure_clean_accepts_clean_tree(tmp_path: Path) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")

    GitRepo(repo_path).ensure_clean()


def test_default_branch_falls_back_to_master_when_main_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        _ = check
        if args == ("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"):
            return completed(*args, returncode=1)
        if args == ("rev-parse", "--verify", "--quiet", "refs/heads/main"):
            return completed(*args, returncode=1)
        if args == ("rev-parse", "--verify", "--quiet", "refs/heads/master"):
            return completed(*args, returncode=0)
        raise AssertionError(args)

    monkeypatch.setattr(repo, "run", fake_run)

    assert repo.default_branch() == "master"


def test_default_branch_uses_single_local_branch_when_it_is_the_only_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        _ = check
        if args == ("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"):
            return completed(*args, returncode=1)
        if args == ("rev-parse", "--verify", "--quiet", "refs/heads/main"):
            return completed(*args, returncode=1)
        if args == ("rev-parse", "--verify", "--quiet", "refs/heads/master"):
            return completed(*args, returncode=1)
        if args == ("branch", "--show-current"):
            return completed(*args, stdout="\n")
        if args == (
            "for-each-ref",
            "--format=%(refname:short)",
            "--points-at",
            "HEAD",
            "refs/heads",
        ):
            return completed(*args, returncode=0, stdout="\n")
        if args == ("for-each-ref", "--format=%(refname:short)", "refs/heads"):
            return completed(*args, returncode=0, stdout="topic\n")
        raise AssertionError(args)

    monkeypatch.setattr(repo, "run", fake_run)

    assert repo.default_branch() == "topic"


def test_default_branch_ignores_ambiguous_local_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fake_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        _ = check
        if args == ("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"):
            return completed(*args, returncode=1)
        if args == ("rev-parse", "--verify", "--quiet", "refs/heads/main"):
            return completed(*args, returncode=1)
        if args == ("rev-parse", "--verify", "--quiet", "refs/heads/master"):
            return completed(*args, returncode=1)
        if args == ("branch", "--show-current"):
            return completed(*args, stdout="\n")
        if args == (
            "for-each-ref",
            "--format=%(refname:short)",
            "--points-at",
            "HEAD",
            "refs/heads",
        ):
            return completed(*args, returncode=0, stdout="\n")
        if args == ("for-each-ref", "--format=%(refname:short)", "refs/heads"):
            return completed(*args, returncode=0, stdout="alpha\nbeta\n")
        raise AssertionError(args)

    monkeypatch.setattr(repo, "run", fake_run)

    assert repo.default_branch() == "main"


def test_checkout_or_create_branch_is_noop_when_already_on_branch(
    tmp_path: Path,
) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")

    GitRepo(repo_path).checkout_or_create_branch("main")

    assert git(repo_path, "branch", "--show-current") == "main"


def test_checkout_or_create_branch_checks_out_existing_branch(tmp_path: Path) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")
    git(repo_path, "checkout", "-b", "feature")
    git(repo_path, "checkout", "main")

    GitRepo(repo_path).checkout_or_create_branch("feature")

    assert git(repo_path, "branch", "--show-current") == "feature"


def test_checkout_or_create_branch_creates_missing_branch(tmp_path: Path) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")

    GitRepo(repo_path).checkout_or_create_branch("topic")

    assert git(repo_path, "branch", "--show-current") == "topic"


def test_commit_paths_if_needed_raises_on_commit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fail_commit_paths_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        if args == ("status", "--short", "--", "README.md"):
            return completed(*args, stdout=" M README.md\n")
        if args == ("add", "-A", "--", "README.md"):
            return completed(*args)
        if args == ("commit", "-m", "save work", "--", "README.md"):
            return completed(*args, returncode=1, stderr="no commit\n")
        raise AssertionError(args)

    monkeypatch.setattr(repo, "run", fail_commit_paths_run)

    with pytest.raises(JriError, match="no commit"):
        repo.commit_paths_if_needed("save work", ["README.md", "README.md"])


def test_commit_paths_if_needed_commits_scoped_paths(tmp_path: Path) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")
    (repo_path / "README.md").write_text("# changed\n", encoding="utf-8")

    assert GitRepo(repo_path).commit_paths_if_needed("save work", ["README.md"]) is True
    assert git(repo_path, "log", "-1", "--pretty=%s") == "save work"


def test_commit_paths_if_needed_skips_clean_scoped_paths(tmp_path: Path) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")

    assert (
        GitRepo(repo_path).commit_paths_if_needed("save work", ["README.md"]) is False
    )


def test_path_matches_head_raises_on_diff_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)
    path = tmp_path / "tracked.txt"

    def fail_diff_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        if args == ("ls-files", "--error-unmatch", "--", "tracked.txt"):
            return completed(*args, returncode=0)
        if args == ("diff", "--quiet", "HEAD", "--", "tracked.txt"):
            return completed(*args, returncode=2, stderr="diff failed\n")
        raise AssertionError(args)

    monkeypatch.setattr(repo, "run", fail_diff_run)

    with pytest.raises(JriError, match="diff failed"):
        repo.path_matches_head(path)


def test_path_matches_head_returns_true_for_untracked_path(tmp_path: Path) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")
    path = repo_path / "untracked.txt"
    path.write_text("draft\n", encoding="utf-8")

    assert GitRepo(repo_path).path_matches_head(path) is True


def test_path_matches_head_returns_true_for_tracked_unchanged_path(
    tmp_path: Path,
) -> None:
    repo_path = make_git_repo(tmp_path, branch="main")

    assert GitRepo(repo_path).path_matches_head(repo_path / "README.md") is True


def test_merge_ff_only_failure_uses_git_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fail_merge_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=1, stderr="merge rejected\n")

    monkeypatch.setattr(repo, "run", fail_merge_run)

    with pytest.raises(JriError, match="merge rejected"):
        repo.merge_ff_only("feature")


def test_merge_ff_only_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def ok_merge_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args)

    monkeypatch.setattr(repo, "run", ok_merge_run)

    repo.merge_ff_only("feature")


def test_merge_no_ff_failure_aborts_and_uses_fallback_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fail_merge_no_ff_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        calls.append(args)
        if args == ("merge", "--no-ff", "-m", "merge feature", "feature"):
            return completed(*args, returncode=1)
        if args == ("merge", "--abort"):
            return completed(*args)
        raise AssertionError(args)

    monkeypatch.setattr(repo, "run", fail_merge_no_ff_run)

    with pytest.raises(JriError, match="failed to merge feature"):
        repo.merge_no_ff("feature", message="merge feature")

    assert calls[-1] == ("merge", "--abort")


def test_merge_no_ff_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = GitRepo(tmp_path)

    def ok_merge_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args)

    monkeypatch.setattr(repo, "run", ok_merge_run)

    repo.merge_no_ff("feature", message="merge feature")


def test_create_tag_failure_uses_git_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fail_tag_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=1, stderr="tag failed\n")

    monkeypatch.setattr(repo, "run", fail_tag_run)

    with pytest.raises(JriError, match="tag failed"):
        repo.create_tag("v1.0.0")


def test_create_tag_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = GitRepo(tmp_path)

    def ok_tag_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args)

    monkeypatch.setattr(repo, "run", ok_tag_run)

    repo.create_tag("v1.0.0")


@pytest.mark.parametrize("returncode, expected", [(0, True), (1, False)])
def test_has_tag_returns_boolean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    expected: bool,
) -> None:
    repo = GitRepo(tmp_path)

    def fake_has_tag_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=returncode)

    monkeypatch.setattr(repo, "run", fake_has_tag_run)

    assert repo.has_tag("v1.0.0") is expected


def test_has_remote_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fake_has_remote_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args)

    monkeypatch.setattr(repo, "run", fake_has_remote_run)

    assert repo.has_remote() is False


def test_is_ancestor_returns_false_and_raises_for_unexpected_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def false_then_error_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        if args == ("merge-base", "--is-ancestor", "a", "b"):
            return completed(*args, returncode=1)
        if args == ("merge-base", "--is-ancestor", "c", "d"):
            return completed(*args, returncode=2, stderr="merge-base failed\n")
        raise AssertionError(args)

    monkeypatch.setattr(repo, "run", false_then_error_run)

    assert repo.is_ancestor("a", "b") is False

    with pytest.raises(JriError, match="merge-base failed"):
        repo.is_ancestor("c", "d")


def test_is_ancestor_returns_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def true_ancestor_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=0)

    monkeypatch.setattr(repo, "run", true_ancestor_run)

    assert repo.is_ancestor("a", "b") is True


def test_has_remote_returns_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fake_has_remote_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, stdout="origin\n")

    monkeypatch.setattr(repo, "run", fake_has_remote_run)

    assert repo.has_remote() is True


def test_push_task_refs_raises_when_a_push_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fail_push_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        if args == ("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"):
            return completed(*args, returncode=1)
        if args == ("rev-parse", "--verify", "--quiet", "refs/heads/main"):
            return completed(*args, returncode=0)
        if args == ("push", "origin", "main"):
            return completed(*args)
        if args == ("push", "origin", "feature"):
            return completed(*args, returncode=1, stderr="push rejected\n")
        if args == ("push", "origin", "v1.0.0"):
            raise AssertionError("tags must not be pushed")
        raise AssertionError(args)

    monkeypatch.setattr(repo, "run", fail_push_run)

    with pytest.raises(JriError, match="push rejected"):
        repo.push_task_refs(branch="feature")


def test_push_task_refs_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def ok_push_run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        _ = check
        if args == ("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"):
            return completed(*args, returncode=1)
        if args == ("rev-parse", "--verify", "--quiet", "refs/heads/main"):
            return completed(*args, returncode=0)
        return completed(*args)

    monkeypatch.setattr(repo, "run", ok_push_run)

    repo.push_task_refs(branch="feature")


def test_diff_returns_stdout_for_non_error_returncodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fake_diff_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=1, stdout="diff output\n")

    monkeypatch.setattr(repo, "run", fake_diff_run)

    assert repo.diff("HEAD~1", "HEAD") == "diff output\n"


def test_diff_raises_on_unexpected_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fail_diff_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=2, stderr="diff boom\n")

    monkeypatch.setattr(repo, "run", fail_diff_run)

    with pytest.raises(JriError, match="diff boom"):
        repo.diff("HEAD~1", "HEAD")


def test_reset_hard_failure_uses_git_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fail_reset_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=1, stderr="reset failed\n")

    monkeypatch.setattr(repo, "run", fail_reset_run)

    with pytest.raises(JriError, match="reset failed"):
        repo.reset_hard("HEAD~1")


def test_reset_hard_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = GitRepo(tmp_path)

    def ok_reset_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args)

    monkeypatch.setattr(repo, "run", ok_reset_run)

    repo.reset_hard("HEAD~1")


def test_reset_branch_failure_uses_fallback_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fail_reset_branch_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=1)

    monkeypatch.setattr(repo, "run", fail_reset_branch_run)

    with pytest.raises(JriError, match="failed to reset branch feature to HEAD"):
        repo.reset_branch("feature", "HEAD")


def test_reset_branch_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = GitRepo(tmp_path)

    def ok_reset_branch_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args)

    monkeypatch.setattr(repo, "run", ok_reset_branch_run)

    repo.reset_branch("feature", "HEAD")


def test_add_worktree_failure_uses_fallback_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fail_add_worktree_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=1)

    monkeypatch.setattr(repo, "run", fail_add_worktree_run)

    with pytest.raises(JriError, match="failed to create worktree"):
        repo.add_worktree(tmp_path / "worktree", "feature")


def test_add_worktree_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = GitRepo(tmp_path)

    def ok_worktree_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args)

    monkeypatch.setattr(repo, "run", ok_worktree_run)

    repo.add_worktree(tmp_path / "worktree", "feature")


def test_prune_worktrees_failure_uses_fallback_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fail_prune_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=1)

    monkeypatch.setattr(repo, "run", fail_prune_run)

    with pytest.raises(JriError, match="failed to prune worktrees"):
        repo.prune_worktrees()


def test_prune_worktrees_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def ok_prune_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args)

    monkeypatch.setattr(repo, "run", ok_prune_run)

    repo.prune_worktrees()


def test_remove_worktree_failure_uses_fallback_message_when_path_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    def fail_remove_worktree_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=1)

    monkeypatch.setattr(repo, "run", fail_remove_worktree_run)

    with pytest.raises(JriError, match="failed to remove worktree"):
        repo.remove_worktree(worktree_path)


def test_remove_worktree_ignores_failure_when_path_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)
    worktree_path = tmp_path / "worktree"

    def fail_remove_worktree_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=1)

    monkeypatch.setattr(repo, "run", fail_remove_worktree_run)

    repo.remove_worktree(worktree_path)


def test_rev_parse_failure_uses_fallback_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = GitRepo(tmp_path)

    def fail_rev_parse_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, returncode=1)

    monkeypatch.setattr(repo, "run", fail_rev_parse_run)

    with pytest.raises(JriError, match="failed to resolve HEAD"):
        repo.rev_parse("HEAD")


def test_rev_parse_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = GitRepo(tmp_path)

    def ok_rev_parse_run(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _ = check
        return completed(*args, stdout="abc123\n")

    monkeypatch.setattr(repo, "run", ok_rev_parse_run)

    assert repo.rev_parse("HEAD") == "abc123"
