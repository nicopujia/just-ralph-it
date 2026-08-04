import logging
import shutil
import socket
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Never

import pytest

from jri.lib import git
from tests.doubles.models import serve_catalog

type CreateRepository = Callable[[Path], git.Repository]
type RunGit = Callable[..., str]


@pytest.fixture
def run_git() -> RunGit:
    """Run Git commands inside a worktree.

    Returns:
        A callable taking a worktree path and Git arguments, and
        returning the command's trimmed standard output.
    """

    executable = shutil.which("git")
    assert executable is not None

    def run(path: Path, *arguments: str) -> str:
        return subprocess.run(
            [executable, "-C", str(path), *arguments], check=True, capture_output=True, text=True
        ).stdout.strip()

    return run


@pytest.fixture
def create_repository(run_git: RunGit) -> CreateRepository:
    """Create repositories holding a single committed file.

    Returns:
        A callable taking a path and returning the repository made
        at it.
    """

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


@pytest.fixture(autouse=True, scope="session")
def block_network() -> Iterator[None]:
    def guard(*_: object, **__: object) -> Never:
        raise OSError("Tests must not use the network.")

    blocked = ("getaddrinfo", "create_connection", "socket")
    originals = {name: getattr(socket, name) for name in blocked}
    for name in blocked:
        setattr(socket, name, guard)
    yield
    for name, original in originals.items():
        setattr(socket, name, original)


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
