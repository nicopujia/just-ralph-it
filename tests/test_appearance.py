import subprocess

import pytest

from jri.lib import appearance


def test_reads_a_dark_system_appearance(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = _serve(monkeypatch, system="Darwin", reported="Dark\n")

    assert appearance.read_appearance() == "dark"
    assert commands == [appearance.DARWIN_COMMAND]


def test_reads_a_light_system_appearance(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, system="Darwin", reported="")

    assert appearance.read_appearance() == "light"


def test_falls_back_to_dark_where_the_system_reports_no_appearance(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = _serve(monkeypatch, system="Linux", reported="Dark\n")

    assert appearance.read_appearance() == "dark"
    assert commands == []


def _serve(monkeypatch: pytest.MonkeyPatch, *, system: str, reported: str) -> list[object]:
    commands: list[object] = []

    def run(command: object, **_options: object) -> "subprocess.CompletedProcess[str]":
        commands.append(command)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=reported, stderr="")

    monkeypatch.setattr(appearance.platform, "system", lambda: system)
    monkeypatch.setattr(appearance.subprocess, "run", run)
    return commands
