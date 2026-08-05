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
from tests.doubles.models import serve_catalog

type CreateRepository = Callable[[Path], git.Repository]
type ReadCredential = Callable[[str], str]
type RunGit = Callable[..., str]

# The file `jri chat` reads its keys from, so a live call is paid for
# by the same credential the product uses.
ENV_FILE = Path(__file__).parent.parent / ".env"
NETWORK = tuple(
    (module, name, getattr(module, name))
    for module, name in ((socket, "getaddrinfo"), (socket, "create_connection"), (socket, "socket"), (httpx, "get"))
)


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


@pytest.fixture(autouse=True)
def isolate_model_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_catalog(monkeypatch)


# A JRI command runs inside the project it works on, so the code under
# test reads the working directory rather than being told about it.
@pytest.fixture(autouse=True)
def isolate_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True, scope="session")
def block_network() -> Iterator[None]:
    def guard(*_: object, **__: object) -> Never:
        raise OSError("Tests must not use the network.")

    for module, name, _ in NETWORK:
        setattr(module, name, guard)
    yield
    for module, name, original in NETWORK:
        setattr(module, name, original)


# A wire contract is the one belief a double cannot falsify, so the
# tests that check one reach the endpoint itself.
@pytest.fixture
def reach_network(monkeypatch: pytest.MonkeyPatch) -> None:
    for module, name, original in NETWORK:
        monkeypatch.setattr(module, name, original)


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
