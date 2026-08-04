import subprocess

import pytest

from jri.lib import appearance


def serve_appearance(monkeypatch: pytest.MonkeyPatch, *, system: str, reported: str) -> list[object]:
    commands: list[object] = []

    def run(command: object, **_options: object) -> "subprocess.CompletedProcess[str]":
        commands.append(command)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=reported, stderr="")

    monkeypatch.setattr(appearance.platform, "system", lambda: system)
    monkeypatch.setattr(appearance.subprocess, "run", run)
    return commands
