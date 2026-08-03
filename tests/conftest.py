import logging
import socket
from collections.abc import Iterator
from typing import Never

import pytest

from tests.doubles.models import serve_catalog


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
