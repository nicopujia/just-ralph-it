"""Black-box TTY CLI harness for functional tests."""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

import pexpect

from tests.support.cli_result import CliRun, archive_debug_logs

_PROMPT_PATTERN = "jri> "
_EOF_PATTERN = cast("object", pexpect.EOF)
_DEFAULT_DIMENSIONS = (24, 100)


class _SpawnedCli(Protocol):
    before: object
    after: object
    exitstatus: int | None
    signalstatus: int | None

    def expect(self, pattern: object) -> int: ...

    def send(self, text: str) -> int: ...

    def sendcontrol(self, char: str) -> int: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class CliTtyHarness:
    """PTY runner for the installed JRI command."""

    command: str
    env: Mapping[str, str]
    timeout: int

    def spawn(
        self,
        *,
        cwd: Path,
        args: tuple[str, ...] = (),
        dimensions: tuple[int, int] = _DEFAULT_DIMENSIONS,
    ) -> "CliTtySession":
        """Spawn the interactive CLI attached to a pseudo-terminal."""
        child = cast(
            "_SpawnedCli",
            cast(
                "object",
                pexpect.spawn(
                    self.command,
                    args=list(args),
                    cwd=str(cwd),
                    env=_terminal_env(self.env),
                    encoding="utf-8",
                    timeout=self.timeout,
                    dimensions=dimensions,
                ),
            ),
        )
        return CliTtySession(cwd=cwd, child=child)

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


@dataclass
class CliTtySession:
    """Interactive PTY session for a running JRI command."""

    cwd: Path
    child: _SpawnedCli
    _output_parts: list[str] = field(default_factory=list)

    def expect_prompt(self) -> None:
        """Wait until the JRI prompt is visible."""
        self._expect(_PROMPT_PATTERN)

    def sendline(self, text: str) -> None:
        """Send one user input line."""
        self.child.send(text)
        self.child.send("\r")

    def send_eof(self) -> None:
        """Send terminal EOF."""
        self.child.send("\x04")

    def send_interrupt(self) -> None:
        """Send terminal interrupt."""
        self.child.sendcontrol("c")

    def expect_eof(self) -> CliRun:
        """Wait for process exit and return the captured CLI result."""
        self._expect(_EOF_PATTERN)
        return self.result()

    def result(self) -> CliRun:
        """Return the captured CLI result."""
        self.child.close()
        stdout = "".join(self._output_parts)
        returncode = _returncode(
            exitstatus=self.child.exitstatus,
            signalstatus=self.child.signalstatus,
        )
        debug_log_dir = archive_debug_logs(
            cwd=self.cwd,
            stdout=stdout,
            stderr="",
            returncode=returncode,
        )
        return CliRun(
            cwd=self.cwd,
            returncode=returncode,
            stdout=stdout,
            stderr="",
            debug_log_dir=debug_log_dir,
        )

    def _expect(self, pattern: object) -> None:
        self.child.expect(pattern)
        self._record_latest_output()

    def _record_latest_output(self) -> None:
        self._output_parts.append(_text(self.child.before))
        self._output_parts.append(_text(self.child.after))


def _terminal_env(env: Mapping[str, str]) -> dict[str, str]:
    terminal_env = dict(env)
    terminal_env.setdefault("TERM", "xterm-256color")
    terminal_env.setdefault("PROMPT_TOOLKIT_NO_CPR", "1")
    return terminal_env


def _returncode(*, exitstatus: int | None, signalstatus: int | None) -> int:
    if exitstatus is not None:
        return exitstatus
    if signalstatus is not None:
        return 128 + signalstatus
    return 1


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return ""
