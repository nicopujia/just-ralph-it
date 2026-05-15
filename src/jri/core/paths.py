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
    def graph_dir(self) -> Path:
        return self.jri_dir / "graph"

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
    def chat_logs_dir(self) -> Path:
        return self.logs_dir / "chat"

    @property
    def external_pi_dir(self) -> Path:
        return self.external_logs_dir / "pi"

    @property
    def diffs_dir(self) -> Path:
        return self.logs_dir / "diffs"

    @property
    def state_path(self) -> Path:
        return self.jri_dir / "state.json"

    @property
    def gitignore_path(self) -> Path:
        return self.jri_dir / ".gitignore"

    @property
    def root_gitignore_path(self) -> Path:
        return self.root / ".gitignore"

    @property
    def readme_path(self) -> Path:
        return self.root / "README.md"

    @property
    def stop_signal_path(self) -> Path:
        return self.signals_dir / "stop"

    @property
    def recovery_log_path(self) -> Path:
        return self.logs_dir / "recovery.log"

    @property
    def recovery_failures_log_path(self) -> Path:
        return self.logs_dir / "recovery-failures.log"

    def task_path(self, status: str, slug: str) -> Path:
        return self.task_dir(status) / f"{slug}.md"

    def graph_node_path(self, semantic_path: str) -> Path:
        from .graph import graph_node_path

        return graph_node_path(self.root, semantic_path)

    def ralph_log_path(self, task_slug: str, started_at: int) -> Path:
        timestamp = datetime.fromtimestamp(started_at, UTC).strftime(
            "%Y-%m-%dT%H-%M-%SZ"
        )
        return self.ralph_logs_dir / f"{timestamp}-{task_slug}.log"

    @property
    def timeline_path(self) -> Path:
        return self.logs_dir / "timeline.jsonl"

    @property
    def metrics_path(self) -> Path:
        return self.jri_dir / "metrics.json"

    @property
    def learnings_path(self) -> Path:
        return self.jri_dir / "learnings.md"

    @property
    def attempts_dir(self) -> Path:
        return self.jri_dir / "attempts"

    @property
    def worktree_dir(self) -> Path:
        return self.jri_dir / "worktree"

    def diff_artifact_path(self, slug: str) -> Path:
        return self.diffs_dir / f"{slug}.diff"

    def attempt_history_path(self, slug: str) -> Path:
        return self.attempts_dir / f"{slug}.yaml"
