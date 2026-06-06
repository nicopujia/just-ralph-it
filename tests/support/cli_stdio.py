"""Black-box stdio CLI harness for functional tests."""

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tests.support.cli_result import CliRun, archive_debug_logs


@dataclass(frozen=True)
class CliStdioHarness:
    """Subprocess runner for the installed JRI command."""

    command: str
    env: Mapping[str, str]
    timeout: int

    def run(
        self,
        *,
        cwd: Path,
        input_text: str = "",
        args: tuple[str, ...] = (),
    ) -> CliRun:
        """Invoke the interactive CLI through stdio pipes."""
        result = subprocess.run(
            [self.command, *args],
            check=False,
            capture_output=True,
            cwd=cwd,
            input=input_text,
            text=True,
            env=dict(self.env),
            timeout=self.timeout,
        )
        debug_log_dir = archive_debug_logs(
            cwd=cwd,
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
        )
        return CliRun(
            cwd=cwd,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            debug_log_dir=debug_log_dir,
        )

    def run_help(self) -> CliRun:
        """Invoke CLI help."""
        cwd = Path.cwd()
        return self.run(cwd=cwd, args=("--help",))

    def initialize_git_repo(self, path: Path) -> None:
        """Initialize a test repository with commit identity configured."""
        subprocess.run(
            ["git", "init"],
            cwd=path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "jri@example.com"],
            cwd=path,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "JRI Tests"],
            cwd=path,
            check=True,
        )
