import logging
import os
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Never

import httpx
import pytest
from dotenv import dotenv_values

from jri.lib import git
from jri.lib.models import get_context_limit
from tests.doubles.models import serve_catalog

type CreateRepository = Callable[[Path], git.Repository]
type ReadCredential = Callable[[str], str]
type RunGit = Callable[..., str]

# The mark a test wears to reach the endpoint it is about, and the only
# thing that lets one through.
CONTRACT_MARKER = "contract"
# The file `jri chat` reads its keys from, so a live call is paid for
# by the same credential the product uses.
ENV_FILE = Path(__file__).parent.parent / ".env"
NETWORK = ((socket, "getaddrinfo"), (socket, "create_connection"), (socket, "socket"), (httpx, "get"))


@pytest.fixture
def run_git() -> RunGit:
    executable = shutil.which("git")
    assert executable is not None

    # Git reports the states it stops in as failures, so reaching one
    # means running a command that is meant to come back non-zero.
    def run(path: Path, *arguments: str, check: bool = True) -> str:
        return subprocess.run(
            [executable, "-C", str(path), *arguments], check=check, capture_output=True, text=True
        ).stdout.strip()

    return run


@pytest.fixture
def create_repository(run_git: RunGit) -> CreateRepository:
    def create(path: Path) -> git.Repository:
        path.mkdir(parents=True, exist_ok=True)
        run_git(path, "init", "-q")
        (path / "README.md").write_text("# Project\n")
        run_git(path, "add", "README.md")
        run_git(path, "commit", "-qm", "initial")
        return git.Repository(path)

    return create


# The outside world answers with what this repository wrote, so no test
# needs a network to be deterministic. The one belief a double cannot
# falsify is the wire contract it is the oracle for, so a test marked
# `contract` -- and only such a test -- reaches the endpoint itself.
@pytest.fixture(autouse=True)
def isolate_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker(CONTRACT_MARKER):
        # Nothing else clears it, and a limit an earlier test cached
        # would answer in the endpoint's place.
        get_context_limit.cache_clear()
        return

    def guard(*_: object, **__: object) -> Never:
        raise OSError("Tests must not use the network.")

    for module, name in NETWORK:
        monkeypatch.setattr(module, name, guard)
    serve_catalog(monkeypatch)


# A JRI command runs inside the project it works on, so the code under
# test reads the working directory rather than being told about it.
@pytest.fixture(autouse=True)
def isolate_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def read_credential() -> ReadCredential:
    def read(variable: str) -> str:
        value = os.environ.get(variable) or dotenv_values(ENV_FILE).get(variable)
        if not value:
            pytest.skip(f"{variable} is unset, so nothing here can pay for a live call")
        return value

    return read


@pytest.fixture(autouse=True)
def isolate_logging() -> Iterator[None]:
    logger = logging.getLogger("jri")
    level, propagate, existing = logger.level, logger.propagate, list(logger.handlers)
    yield
    for handler in logger.handlers:
        if handler not in existing and isinstance(handler, logging.FileHandler):
            handler.close()
    logger.handlers, logger.level, logger.propagate = existing, level, propagate


@pytest.fixture(autouse=True)
def isolate_git(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path_factory.mktemp("git") / "config"
    config.write_text("[core]\n\texcludesFile = /dev/null\n\tattributesFile = /dev/null\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test User")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test User")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
