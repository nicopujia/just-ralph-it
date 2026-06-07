"""Repository hygiene tests for generated local artifacts."""

import subprocess
from pathlib import Path


def test_jri_test_runs_directory_is_ignored() -> None:
    """Git ignores newly generated JRI test-run artifacts."""
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", ".jri-test-runs/example"],
        cwd=_repo_root(),
        check=False,
    )

    assert result.returncode == 0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]
