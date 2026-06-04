"""Integration tests for the command line interface."""

import shutil
import subprocess


def test_cli_introduces_app() -> None:
    """The CLI greets users with the app name."""
    jri = shutil.which("jri")
    assert jri is not None

    result = subprocess.run(
        [jri],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == "Just Ralph It\n"
    assert not result.stderr
