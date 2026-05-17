import os
import subprocess
import sys
from pathlib import Path


def _entrypoint_env() -> dict[str, str]:
    return os.environ | {"PYTHONPATH": str(Path(__file__).parents[2] / "src")}


def test_jri_package_entrypoint_shows_cli_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "jri", "--help"], capture_output=True, text=True, check=False, env=_entrypoint_env()
    )

    assert result.returncode == 0
    assert "Manage a project and run the Ralph task loop." in result.stdout
    assert "start" in result.stdout
    assert result.stderr == ""


def test_tools_package_entrypoint_reports_missing_git_repo(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "jri.core.agents.bundle._shared.tools", "list-tasks"],
        input="{}",
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=_entrypoint_env(),
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "jri requires a git repository\n"
