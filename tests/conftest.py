import logging
import os
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Never, cast

import httpx
import pytest
from dotenv import dotenv_values

from jri.core.settings import LLM
from jri.lib import git
from jri.lib.models import read_context_limit
from tests.doubles.models import serve_catalog

type CreateLink = Callable[[Path, Path], None]
type CreateRepository = Callable[[Path], git.Repository]
type ReadCredential = Callable[[str], str]
type RunGit = Callable[..., str]

CONTRACT_MARKER = "contract"
# This file gives `jri chat` its API keys.
# A live call uses the same credential as the product.
ENV_FILE = Path(__file__).parent.parent / ".env"
NETWORK = ((socket, "getaddrinfo"), (socket, "create_connection"), (socket, "socket"), (httpx, "get"))


@pytest.fixture
def run_git() -> RunGit:
    executable = shutil.which("git")
    assert executable is not None

    # Git reports its stop states as failures.
    # Run a command with a nonzero result to reach one state.
    def run(path: Path, *arguments: str, check: bool = True) -> str:
        return subprocess.run(
            [executable, "-C", str(path), *arguments], check=check, capture_output=True, text=True
        ).stdout.strip()

    return run


@pytest.fixture
def create_link() -> CreateLink:
    def create(path: Path, target: Path) -> None:
        try:
            # Windows distinguishes directory links from file links.
            # It does not follow a link of the other type.
            path.symlink_to(target, target_is_directory=target.is_dir())
        except OSError as error:
            pytest.skip(f"a link needs a privilege this machine withholds: {error}")

    return create


@pytest.fixture
def create_repository(run_git: RunGit) -> CreateRepository:
    def create(path: Path) -> git.Repository:
        path.mkdir(parents=True, exist_ok=True)
        run_git(path, "init", "-q")
        # Git and these assertions read the stored bytes.
        # The platform line ending is not part of JRI data.
        (path / "README.md").write_bytes(b"# Project\n")
        run_git(path, "add", "README.md")
        run_git(path, "commit", "-qm", "initial")
        return git.Repository(path)

    return create


# The repository provides all external responses for these tests.
# Therefore, normal tests do not need network access.
# A double cannot verify its own wire contract.
# Only a `contract` test calls the real endpoint.
@pytest.fixture(autouse=True)
def isolate_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker(CONTRACT_MARKER):
        # No other fixture clears this cache.
        # A cached limit from another test could replace the endpoint result.
        read_context_limit.cache_clear()
        return

    def guard(*_: object, **__: object) -> Never:
        raise OSError("Tests must not use the network.")

    for module, name in NETWORK:
        monkeypatch.setattr(module, name, guard)
    serve_catalog(monkeypatch)


# A JRI command runs in its project directory.
# The code under test reads the current working directory.
@pytest.fixture(autouse=True)
def isolate_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


# The default settings name the variable that holds the provider key.
# A test that loads the default settings needs this variable.
# Set a test value, because the key in the shell must not change a result.
@pytest.fixture(autouse=True)
def isolate_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cast("str", LLM().api_key), "test-provider-key")


@pytest.fixture
def read_credential() -> ReadCredential:
    # An unpaid contract test does not verify the contract.
    # A release run must fail when its required key is not set.
    # A skip would hide this release failure.
    def read(variable: str) -> str:
        value = os.environ.get(variable) or dotenv_values(ENV_FILE).get(variable)
        if not value:
            pytest.fail(f"{variable} must be set: nothing here can pay for the live call this checks")
        return value

    return read


@pytest.fixture(autouse=True)
def isolate_logging() -> Iterator[None]:
    logger = logging.getLogger("jri")
    level, propagate, existing = logger.level, logger.propagate, list(logger.handlers)
    yield
    for handler in logger.handlers:
        if handler not in existing:
            handler.close()
    logger.handlers, logger.level, logger.propagate = existing, level, propagate


# `GIT_DIR` overrides the repository that a command's `-C` flag names. A test that inherits it from the
# calling shell would run Git against that repository instead of its own `tmp_path` one.
@pytest.fixture(autouse=True)
def isolate_git(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path_factory.mktemp("git") / "config"
    config.write_text(f"[core]\n\texcludesFile = {os.devnull}\n\tattributesFile = {os.devnull}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test User")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test User")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
