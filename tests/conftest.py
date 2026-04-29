from pathlib import Path
from typing import TypedDict, cast

import pytest
from _pytest.capture import CaptureManager

from jri.cli.main import resolve_start_models
from jri.core.agents.presets import preset_choices
from tests.helpers import git


class LiveStartModels(TypedDict):
    model: str | None
    validator_model: str | None
    general_model: str | None
    explore_model: str | None


def _disable_pytest_capture_for_live_runs(config: pytest.Config) -> None:
    if not config.getoption("run_live_pi"):
        return

    config.option.capture = "no"
    pluginmanager = config.pluginmanager
    capturemanager = pluginmanager.getplugin("capturemanager")
    if capturemanager is None:
        return

    pluginmanager.unregister(capturemanager)
    capturemanager.stop_global_capturing()

    live_capturemanager = CaptureManager("no")
    pluginmanager.register(live_capturemanager, "capturemanager")
    live_capturemanager.start_global_capturing()
    config.add_cleanup(live_capturemanager.stop_global_capturing)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "-L",
        "--run-live-pi",
        action="store_true",
        default=False,
        help=(
            "run live tests against a real Pi runtime and disable "
            "pytest capture so agent output streams live"
        ),
    )
    parser.addoption(
        "--preset",
        choices=preset_choices(),
        help=(
            "Apply the named start preset for live Pi tests. "
            "Use 'default' to match the checked-in config."
        ),
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


def pytest_configure(config: pytest.Config) -> None:
    _disable_pytest_capture_for_live_runs(config)


def run_cli(args: list[str], cwd: Path) -> int:
    from jri.cli import main

    return main(args, cwd=cwd)


@pytest.fixture
def run_live_pi(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("run_live_pi"))


@pytest.fixture
def model(request: pytest.FixtureRequest) -> str | None:
    value = request.config.getoption("model")
    assert isinstance(value, str) or value is None
    return value


@pytest.fixture
def preset(request: pytest.FixtureRequest) -> str | None:
    value = request.config.getoption("preset")
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
def live_start_models(
    preset: str | None,
    model: str | None,
    validator_model: str | None,
    general_model: str | None,
    explore_model: str | None,
) -> LiveStartModels:
    return cast(
        LiveStartModels,
        resolve_start_models(
            preset=preset,
            model=model,
            validator_model=validator_model,
            general_model=general_model,
            explore_model=explore_model,
        ),
    )


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
