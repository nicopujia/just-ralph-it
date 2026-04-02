from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class JriPaths:
    root: Path

    @property
    def jri_dir(self) -> Path:
        return self.root / ".jri"

    @property
    def tasks_dir(self) -> Path:
        return self.jri_dir / "tasks"

    def task_dir(self, status: str) -> Path:
        return self.tasks_dir / status

    @property
    def signals_dir(self) -> Path:
        return self.jri_dir / "signals"

    @property
    def logs_dir(self) -> Path:
        return self.jri_dir / "logs"

    @property
    def ralph_logs_dir(self) -> Path:
        return self.logs_dir / "ralph"

    @property
    def external_logs_dir(self) -> Path:
        return self.logs_dir / "external"

    @property
    def external_opencode_dir(self) -> Path:
        return self.external_logs_dir / "opencode"

    @property
    def state_path(self) -> Path:
        return self.jri_dir / "state.json"

    @property
    def gitignore_path(self) -> Path:
        return self.jri_dir / ".gitignore"

    @property
    def stop_signal_path(self) -> Path:
        return self.signals_dir / "stop"

    @property
    def opencode_agents_dir(self) -> Path:
        return self.root / ".opencode" / "agents"

    def task_path(self, status: str, slug: str) -> Path:
        return self.task_dir(status) / f"{slug}.md"

    def ralph_log_path(self, iteration: int, started_at: int) -> Path:
        timestamp = datetime.fromtimestamp(started_at, UTC).strftime(
            "%Y-%m-%dT%H-%M-%SZ"
        )
        return self.ralph_logs_dir / f"{iteration}-{timestamp}.log"
