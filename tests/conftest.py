"""Shared pytest configuration."""

import os
from pathlib import Path

import pytest

from tests.env import INTERVIEWER_FACTORY_ENV
from tests.support.cli import CliHarness


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add integration-test options."""
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run integration tests with real model providers",
    )


@pytest.fixture
def live(request: pytest.FixtureRequest) -> bool:
    """Return whether tests should use live external providers."""
    return bool(request.config.getoption("--live"))


@pytest.fixture
def runtime_env(live: bool) -> dict[str, str]:
    """Return an environment for tests that use external providers."""
    env = os.environ.copy()
    if live:
        env.update(_read_dotenv(Path.cwd() / ".env"))
    return env


@pytest.fixture
def cli_env(live: bool, runtime_env: dict[str, str]) -> dict[str, str]:
    """Return an environment for CLI integration tests."""
    env = runtime_env.copy()
    if live:
        env.pop(INTERVIEWER_FACTORY_ENV, None)
        if not env.get("OPENROUTER_API_KEY"):
            pytest.fail("OPENROUTER_API_KEY is required for --live")
    else:
        env.pop("OPENROUTER_API_KEY", None)
        env[INTERVIEWER_FACTORY_ENV] = (
            "tests.doubles.interviewers:create_scripted_interviewer"
        )
        env["PYTHONPATH"] = _prepend_pythonpath(
            Path(__file__).resolve().parents[1],
            env.get("PYTHONPATH"),
        )
    return env


@pytest.fixture
def cli(cli_env: dict[str, str], cli_run_timeout: int) -> CliHarness:
    """Return a black-box harness for CLI integration tests."""
    return CliHarness(
        command=_jri_path(),
        env=cli_env,
        timeout=cli_run_timeout,
    )


@pytest.fixture
def credentialless_cli() -> CliHarness:
    """Return a CLI harness without model credentials or test doubles."""
    env = os.environ.copy()
    env.pop("OPENROUTER_API_KEY", None)
    env.pop(INTERVIEWER_FACTORY_ENV, None)
    return CliHarness(command=_jri_path(), env=env, timeout=30)


@pytest.fixture
def cli_run_timeout(live: bool) -> int:
    """Return a subprocess timeout for CLI integration tests."""
    return 180 if live else 30


@pytest.fixture
def first_turn_input() -> str:
    """Return a simple first-turn interview script."""
    return "I want a tiny CLI that prints hello to stdout.\n"


@pytest.fixture
def early_trigger_input() -> str:
    """Return an under-specified trigger attempt."""
    return "Build a useful software product.\njust ralph it\n"


@pytest.fixture
def mvp_happy_path_input() -> str:
    """Return a complete MVP interview script."""
    return (
        "I want to define a first-version software project: a tiny CLI "
        "called hello-cli. It has one workflow: the user runs "
        "`hello-cli`; it prints exactly `hello` followed by a newline "
        "to stdout and exits 0. Primary user: me, a developer checking "
        "that JRI captured the spec. Inputs: no args and no stdin. "
        "Outputs: only stdout text. Persistence: none. Integrations: "
        "none. Config: none. Error behavior: no custom errors in v1. "
        "Edge cases: extra arguments may be ignored. Non-goals: "
        "packaging, colors, prompts, network, files, and deployment. "
        "Success: an engineer can implement one command with one "
        "unambiguous user-visible result.\n"
        "Answering any remaining clarification: target platform is a "
        "Linux or macOS terminal; command name is hello-cli; output is "
        "lowercase hello plus a trailing newline; no interactive mode; "
        "no dependencies; no hidden state; this is enough for v1.\n"
        "just ralph it\n"
    )


def _jri_path() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    jri = repo_root / ".venv" / "bin" / "jri"
    if not jri.exists():
        pytest.fail(
            f"Repo-local jri console script not found at {jri}. "
            + "Run `uv sync` before integration tests."
        )
    return str(jri)


def _prepend_pythonpath(path: Path, existing: str | None) -> str:
    if not existing:
        return str(path)
    return f"{path}{os.pathsep}{existing}"


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value.strip().strip("'\"")
    return values
