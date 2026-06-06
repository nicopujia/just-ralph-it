"""Tests for the validation helper script."""

import subprocess
from pathlib import Path


def test_validate_help_describes_mode_selection() -> None:
    """The validation script exposes help without running validation."""
    repo_root = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        ["./scripts/validate.py", "--help"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert not result.stderr
    assert "Run the project validation workflow." in result.stdout
    assert "--mode" in result.stdout
    assert "fast" in result.stdout
    assert "smoke" in result.stdout
    assert "full" in result.stdout
