from pathlib import Path

import pytest

from tests.helpers import git


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "-L",
        "--run-live-opencode",
        action="store_true",
        default=False,
        help="run live tests that call the real OpenCode CLI",
    )
    parser.addoption(
        "--model",
        help="Override the Ralph model for live start tests.",
    )
    parser.addoption(
        "--validator-model",
        help="Override the Ralph validator model for live start tests.",
    )
    parser.addoption(
        "--general-model",
        help="Override the general subagent model for live start tests.",
    )
    parser.addoption(
        "--explore-model",
        help="Override the explore subagent model for live start tests.",
    )


def run_cli(args: list[str], cwd: Path) -> int:
    from jri.cli import main

    return main(args, cwd=cwd)


@pytest.fixture
def run_live_opencode(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("run_live_opencode"))


@pytest.fixture
def model(request: pytest.FixtureRequest) -> str | None:
    value = request.config.getoption("model")
    assert isinstance(value, str) or value is None
    return value


@pytest.fixture
def validator_model(request: pytest.FixtureRequest) -> str | None:
    value = request.config.getoption("validator_model")
    assert isinstance(value, str) or value is None
    return value


@pytest.fixture
def general_model(request: pytest.FixtureRequest) -> str | None:
    value = request.config.getoption("general_model")
    assert isinstance(value, str) or value is None
    return value


@pytest.fixture
def explore_model(request: pytest.FixtureRequest) -> str | None:
    value = request.config.getoption("explore_model")
    assert isinstance(value, str) or value is None
    return value


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
