"""Shared pytest configuration."""

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from tests.env import INTERVIEWER_FACTORY_ENV
from tests.support.cli_stdio import CliStdioHarness
from tests.support.cli_tty import CliTtyHarness


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add external-provider test options."""
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run tests with real external providers",
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
        env.update({
            key: value
            for key, value in dotenv_values(Path.cwd() / ".env").items()
            if value is not None
        })
    return env


@pytest.fixture
def cli_env(live: bool, runtime_env: dict[str, str]) -> dict[str, str]:
    """Return an environment for black-box CLI tests."""
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
def cli_stdio(
    cli_env: dict[str, str],
    cli_run_timeout: int,
) -> CliStdioHarness:
    """Return a black-box stdio harness for CLI functional tests."""
    return CliStdioHarness(
        command=_jri_path(),
        env=cli_env,
        timeout=cli_run_timeout,
    )


@pytest.fixture
def cli_tty(
    cli_env: dict[str, str],
    cli_run_timeout: int,
) -> CliTtyHarness:
    """Return a black-box TTY harness for CLI functional tests."""
    return CliTtyHarness(
        command=_jri_path(),
        env=cli_env,
        timeout=cli_run_timeout,
    )


@pytest.fixture
def credentialless_cli_stdio() -> CliStdioHarness:
    """Return a stdio CLI harness without credentials or test doubles."""
    env = os.environ.copy()
    env.pop("OPENROUTER_API_KEY", None)
    env.pop(INTERVIEWER_FACTORY_ENV, None)
    return CliStdioHarness(command=_jri_path(), env=env, timeout=30)


@pytest.fixture
def cli_run_timeout(live: bool) -> int:
    """Return a subprocess timeout for black-box CLI tests."""
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
