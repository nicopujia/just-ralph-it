import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.helpers import write_live_makefile
from tests.live.conftest import show_live_agent_output


class _FakeCaptureManager:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    @contextmanager
    def global_and_fixture_disabled(self):
        self.entered += 1
        try:
            yield
        finally:
            self.exited += 1


def test_live_makefile_passes_without_tests_and_runs_pytest(git_repo: Path) -> None:
    write_live_makefile(git_repo)

    empty_check = subprocess.run(
        ["make", "check"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert empty_check.returncode == 0

    src_dir = git_repo / "src"
    src_dir.mkdir()
    (src_dir / "greet.py").write_text(
        'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n',
        encoding="utf-8",
    )
    tests_dir = git_repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_greet.py").write_text(
        "from greet import greet\n\n"
        "\n"
        "def test_greet() -> None:\n"
        '    assert greet("world") == "nope"\n',
        encoding="utf-8",
    )

    failing_check = subprocess.run(
        ["make", "check"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert failing_check.returncode != 0
    assert "FAILED" in failing_check.stdout


def test_live_output_fixture_disables_global_capture_for_live_runs() -> None:
    capturemanager = _FakeCaptureManager()
    request = SimpleNamespace(
        config=SimpleNamespace(
            pluginmanager=SimpleNamespace(
                getplugin=lambda name: (
                    capturemanager if name == "capturemanager" else None
                )
            )
        )
    )

    fixture_func = cast(Any, show_live_agent_output).__wrapped__
    fixture = fixture_func(
        run_live_opencode=True,
        request=request,
    )

    next(fixture)

    assert capturemanager.entered == 1
    assert capturemanager.exited == 0
    with pytest.raises(StopIteration):
        next(fixture)
    assert capturemanager.exited == 1


def test_live_output_fixture_leaves_non_live_capture_unchanged() -> None:
    capturemanager = _FakeCaptureManager()
    request = SimpleNamespace(
        config=SimpleNamespace(
            pluginmanager=SimpleNamespace(getplugin=lambda name: capturemanager)
        )
    )

    fixture_func = cast(Any, show_live_agent_output).__wrapped__
    fixture = fixture_func(
        run_live_opencode=False,
        request=request,
    )

    next(fixture)

    assert capturemanager.entered == 0
    with pytest.raises(StopIteration):
        next(fixture)
    assert capturemanager.exited == 0
