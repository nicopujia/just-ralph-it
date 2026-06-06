# pyright: reportAny=false
"""Unit tests for CLI entrypoint behavior."""

import importlib
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest


def test_main_exits_nonzero_when_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Initialization errors become non-zero CLI exits."""
    cli_main = importlib.import_module("jri.cli.main")

    def fail_initialize(start: Path, *, force: bool = False) -> object:
        _ = (start, force)
        msg = "init failed"
        raise RuntimeError(msg)

    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.setattr(cli_main, "initialize_project", fail_initialize)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main([])

    assert exc_info.value.code == 1
    assert "init failed" in capsys.readouterr().err


def test_main_validates_runtime_before_initialization(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Missing provider credentials fail before project mutation."""
    cli_main = importlib.import_module("jri.cli.main")
    called = False

    def initialize(start: Path, *, force: bool = False) -> object:
        nonlocal called
        called = True
        _ = (start, force)
        return object()

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("JRI_INTERVIEWER_FACTORY", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli_main, "initialize_project", initialize)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main([])

    assert exc_info.value.code == 1
    assert not called
    assert "OPENROUTER_API_KEY is required" in capsys.readouterr().err


def test_main_loads_project_root_dotenv_before_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Root .env credentials are available when launched from a child."""
    cli_main = importlib.import_module("jri.cli.main")
    project = tmp_path / "project"
    child = project / "app" / "api"
    child.mkdir(parents=True)
    subprocess.run(
        ["git", "init"], cwd=project, check=True, capture_output=True
    )
    (project / ".env").write_text(
        "OPENROUTER_API_KEY=fake\n",
        encoding="utf-8",
    )
    seen_env: dict[str, str] = {}

    def create_interviewer(
        *,
        project_root: Path,
        logger: object,
        env: Mapping[str, str],
    ) -> object:
        _ = (project_root, logger)
        seen_env.update(env)
        return object()

    def run_repl(*, state: object, interviewer: object) -> int:
        _ = (state, interviewer)
        return 0

    monkeypatch.chdir(child)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("JRI_INTERVIEWER_FACTORY", raising=False)
    monkeypatch.setattr(cli_main, "create_interviewer", create_interviewer)
    monkeypatch.setattr(cli_main, "run_repl", run_repl)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main([])

    assert exc_info.value.code == 0
    assert seen_env["OPENROUTER_API_KEY"] == "fake"
    assert (project / ".jri").exists()
    assert not (child / ".jri").exists()
