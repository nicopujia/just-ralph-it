"""Shared result helpers for black-box CLI tests."""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from time import time_ns
from typing import cast

_ANSI_PATTERN = re.compile(rf"{re.escape(chr(27))}\[[0-9;?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class CliRun:
    """Result of invoking the installed JRI command."""

    cwd: Path
    returncode: int
    stdout: str
    stderr: str
    debug_log_dir: Path | None = None

    @property
    def output(self) -> str:
        """Return combined process output."""
        return self.stdout + self.stderr

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
        """Return whether logged assistant text reached process output."""
        visible_output = _normalize_terminal_output(_strip_ansi(self.output))
        return any(
            message.strip()
            and _normalize_terminal_output(message.strip()) in visible_output
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


def archive_debug_logs(
    *,
    cwd: Path,
    stdout: str,
    stderr: str,
    returncode: int,
) -> Path | None:
    """Archive session logs and captured process output for debugging."""
    logs = cwd / ".jri" / "logs"
    if not logs.exists():
        return None

    test_name = os.environ.get("PYTEST_CURRENT_TEST", "manual").split(" ")[0]
    run_name = f"{time_ns()}-{_slug(test_name)}-{_slug(cwd.name)}"
    archive_dir = Path.cwd() / ".jri-test-runs" / run_name
    archive_dir.mkdir(parents=True)
    shutil.copytree(logs, archive_dir / "logs")
    (archive_dir / "cwd.txt").write_text(f"{cwd}\n", encoding="utf-8")
    (archive_dir / "returncode.txt").write_text(
        f"{returncode}\n",
        encoding="utf-8",
    )
    (archive_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    (archive_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    return archive_dir


def _event_data(event: dict[str, object]) -> dict[str, object]:
    return cast("dict[str, object]", event["data"])


def _strip_ansi(text: str) -> str:
    return _ANSI_PATTERN.sub("", text)


def _normalize_terminal_output(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return slug[:120] or "run"
