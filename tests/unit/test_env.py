import os
from pathlib import Path

from jri.core.env import load_repo_env
from jri.core.git import GitRepo, normalize_remote_url
from tests.helpers import git


def test_load_repo_env_reads_missing_values_only(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        'REMOTE_URL="https://github.com/example/justralph.it.git"\n'
        "EXTRA_VALUE=loaded\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("REMOTE_URL", raising=False)
    monkeypatch.setenv("EXTRA_VALUE", "from-os")

    load_repo_env(tmp_path)

    assert os.environ["REMOTE_URL"] == "https://github.com/example/justralph.it.git"
    assert os.environ["EXTRA_VALUE"] == "from-os"


def test_normalize_remote_url_treats_https_and_ssh_as_same_repo() -> None:
    assert normalize_remote_url("https://github.com/example/justralph.it.git") == (
        "github.com/example/justralph.it"
    )
    assert normalize_remote_url("git@github.com:example/justralph.it.git") == (
        "github.com/example/justralph.it"
    )


def test_git_repo_matches_remote_url_after_normalization(git_repo: Path) -> None:
    git(git_repo, "remote", "add", "origin", "git@github.com:example/justralph.it.git")

    repo = GitRepo(git_repo)

    assert repo.matches_remote_url("https://github.com/example/justralph.it") is True
