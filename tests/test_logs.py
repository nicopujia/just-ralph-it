from pathlib import Path
from types import SimpleNamespace

import pytest

from jri.core import logs, paths
from jri.core.conversation import Conversation
from jri.core.exceptions import PersistenceError
from jri.core.workspace import Workspace
from tests.doubles.openai import FakeClient
from tests.doubles.settings import build_settings


def test_writes_a_log_file_for_every_run(tmp_path: Path) -> None:
    Workspace.create(tmp_path)
    settings = build_settings(tmp_path, FakeClient([])).model_copy(update={"logging": SimpleNamespace(level="INFO")})

    logs.configure(settings)
    Conversation(settings)

    files = list((tmp_path / paths.LOGS_DIR).iterdir())
    assert len(files) == 1
    assert "initialized" in files[0].read_text()


def test_explains_when_the_log_file_cannot_be_created(tmp_path: Path) -> None:
    (tmp_path / paths.WORKSPACE_DIR).mkdir()
    (tmp_path / paths.LOGS_DIR).write_text("not a directory")

    with pytest.raises(PersistenceError, match="Could not create the log file"):
        logs.configure(build_settings(tmp_path, FakeClient([])))
