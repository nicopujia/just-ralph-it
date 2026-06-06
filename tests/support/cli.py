"""Black-box CLI harness for integration tests."""

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_ANSI_PATTERN = re.compile(rf"{re.escape(chr(27))}\[[0-9;]*m")


@dataclass(frozen=True)
class CliRun:
    """Result of invoking the installed JRI command."""

    cwd: Path
    returncode: int
    stdout: str
    stderr: str

    @property
    def jri_dir(self) -> Path:
        """Return the project-local JRI state directory."""
        return self.cwd / ".jri"

    def events(self) -> list[dict[str, object]]:
        """Return JSONL events emitted by the CLI session."""
        log = self.jri_dir / "logs" / "interview.jsonl"
        if not log.exists():
            return []
        return [
            cast("dict[str, object]", json.loads(line))
            for line in log.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def event_types(self) -> list[str]:
        """Return event type names from the interview log."""
        return [cast("str", event["type"]) for event in self.events()]

    def user_messages(self) -> list[str]:
        """Return user messages recorded by the interview log."""
        return [
            cast("str", _event_data(event)["message"])
            for event in self.events()
            if event["type"] == "user_message"
        ]

    def assistant_messages(self) -> list[str]:
        """Return assistant messages recorded by the interview log."""
        return [
            cast("str", _event_data(event)["message"])
            for event in self.events()
            if event["type"] == "assistant_message"
        ]

    def has_visible_assistant_output(self) -> bool:
        """Return whether logged assistant text reached stdout."""
        visible_stdout = _strip_ansi(self.stdout)
        return any(
            message.strip() and message.strip() in visible_stdout
            for message in self.assistant_messages()
        )

    def has_assistant_response_after_last_user_message(self) -> bool:
        """Return whether the final user turn received a response."""
        events = self.events()
        user_indexes = [
            index
            for index, event in enumerate(events)
            if event["type"] == "user_message"
        ]
        if not user_indexes:
            return False
        return any(
            event["type"] == "assistant_message"
            for event in events[user_indexes[-1] :]
        )

    def finish_reason(self) -> str:
        """Return the recorded session finish reason, if any."""
        for event in reversed(self.events()):
            if event["type"] == "session_finished":
                return cast("str", _event_data(event)["reason"])
        return ""

    def has_commit(self) -> bool:
        """Return whether the project repository has a HEAD commit."""
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=self.cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def committed_files(self) -> set[str]:
        """Return file paths included in the current HEAD commit."""
        result = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=self.cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return set()
        return {line for line in result.stdout.splitlines() if line}

    def committed_spec_text(self) -> str:
        """Return the concatenated text of committed spec files."""
        specs = [
            self.cwd / path
            for path in sorted(self.committed_files())
            if path.startswith(".jri/specs/") and path.endswith(".md")
        ]
        return "\n".join(
            spec.read_text(encoding="utf-8") for spec in specs if spec.exists()
        )


@dataclass(frozen=True)
class CliHarness:
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
        """Invoke the interactive CLI in a project directory."""
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
        return CliRun(
            cwd=cwd,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
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


def _event_data(event: dict[str, object]) -> dict[str, object]:
    return cast("dict[str, object]", event["data"])


def _strip_ansi(text: str) -> str:
    return _ANSI_PATTERN.sub("", text)
