from pathlib import Path

import pytest

from tests.helpers import git


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live-opencode",
        action="store_true",
        default=False,
        help="run live tests that call the real OpenCode CLI",
    )
    parser.addoption(
        "--opencode-model",
        default="opencode/qwen3.6-plus-free",
        help="model to use for live OpenCode tests",
    )
    parser.addoption(
        "--run-self-hosting-proof",
        action="store_true",
        default=False,
        help="run the end-to-end self-hosting proof test",
    )


def run_cli(args: list[str], cwd: Path) -> int:
    from jri.cli import main

    return main(args, cwd=cwd)


@pytest.fixture
def run_live_opencode(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("run_live_opencode"))


@pytest.fixture
def opencode_model(request: pytest.FixtureRequest) -> str:
    value = request.config.getoption("opencode_model")
    assert isinstance(value, str)
    return value


@pytest.fixture
def run_self_hosting_proof(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("run_self_hosting_proof"))


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "JRI Tests")
    git(repo, "config", "user.email", "jri-tests@example.com")
    (repo / "README.md").write_text("# temp repo\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    return repo
