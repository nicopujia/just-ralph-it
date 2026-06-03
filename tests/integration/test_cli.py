"""Integration tests for the command line interface."""

import pytest

from jri.cli import main


def test_cli_introduces_app(capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI greets users with the app name."""
    main()

    assert capsys.readouterr().out == "Just Ralph It\n"
