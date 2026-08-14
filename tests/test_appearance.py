import pytest

from jri.lib import appearance
from tests.doubles.appearance import serve_appearance


def test_reads_a_dark_system_appearance(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_appearance(monkeypatch, system="Darwin", reported="Dark\n")

    assert appearance.read() == "dark"


def test_reads_a_light_system_appearance(monkeypatch: pytest.MonkeyPatch) -> None:
    # macOS sets `AppleInterfaceStyle` only in dark mode. `defaults read` reports nothing in light mode.
    serve_appearance(monkeypatch, system="Darwin", reported="")

    assert appearance.read() == "light"


def test_reports_no_appearance_where_the_system_does_not_have_one(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = serve_appearance(monkeypatch, system="Linux", reported="Dark\n")

    assert appearance.read() is None
    assert commands == []
