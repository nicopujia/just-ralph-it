import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests.conftest import _disable_pytest_capture_for_live_runs
from tests.helpers import write_live_makefile


class _FakeCaptureManager:
    def __init__(self, method: str = "fd") -> None:
        self.method = method
        self.stop_calls = 0
        self.start_calls = 0

    def stop_global_capturing(self) -> None:
        self.stop_calls += 1

    def start_global_capturing(self) -> None:
        self.start_calls += 1


class _FakePluginManager:
    def __init__(self, capturemanager: _FakeCaptureManager | None) -> None:
        self.capturemanager = capturemanager
        self.unregistered: list[object] = []
        self.registered: list[tuple[object, str]] = []

    def getplugin(self, name: str) -> object | None:
        if name != "capturemanager":
            return None
        return self.capturemanager

    def unregister(self, plugin: object) -> None:
        self.unregistered.append(plugin)
        if plugin is self.capturemanager:
            self.capturemanager = None

    def register(self, plugin: object, name: str) -> None:
        self.registered.append((plugin, name))
        if name == "capturemanager":
            self.capturemanager = cast(_FakeCaptureManager, plugin)


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


def test_live_pytest_config_disables_capture_for_live_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capturemanager = _FakeCaptureManager()
    pluginmanager = _FakePluginManager(capturemanager)
    cleanups: list[object] = []
    config = SimpleNamespace(
        option=SimpleNamespace(capture="fd"),
        pluginmanager=pluginmanager,
        getoption=lambda name: name == "run_live_pi",
        add_cleanup=cleanups.append,
    )

    monkeypatch.setattr(
        "tests.conftest.CaptureManager",
        _FakeCaptureManager,
    )

    _disable_pytest_capture_for_live_runs(cast(Any, config))

    assert config.option.capture == "no"
    assert capturemanager.stop_calls == 1
    assert pluginmanager.unregistered == [capturemanager]
    assert len(pluginmanager.registered) == 1
    replacement, plugin_name = pluginmanager.registered[0]
    assert isinstance(replacement, _FakeCaptureManager)
    assert replacement.method == "no"
    assert replacement.start_calls == 1
    assert plugin_name == "capturemanager"
    assert cleanups == [replacement.stop_global_capturing]


def test_live_pytest_config_leaves_non_live_capture_unchanged() -> None:
    capturemanager = _FakeCaptureManager()
    pluginmanager = _FakePluginManager(capturemanager)
    config = SimpleNamespace(
        option=SimpleNamespace(capture="fd"),
        pluginmanager=pluginmanager,
        getoption=lambda name: False,
        add_cleanup=lambda cleanup: None,
    )

    _disable_pytest_capture_for_live_runs(cast(Any, config))

    assert config.option.capture == "fd"
    assert capturemanager.stop_calls == 0
    assert pluginmanager.unregistered == []
    assert pluginmanager.registered == []
