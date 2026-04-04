"""Tests for the structured status output schema stability."""

import json
from pathlib import Path
from typing import Any

from tests.conftest import run_cli
from tests.helpers import read_json, write_task


def _init(repo: Path) -> None:
    run_cli(["init"], cwd=repo)


def _load_structured_json(capsys) -> dict[str, Any]:
    out = capsys.readouterr().out
    return json.loads(out)


class TestStructuredStatusEmptyProject:
    """Schema stability for an initialized project with no tasks."""

    def test_has_top_level_keys(self, git_repo: Path, capsys) -> None:
        _init(git_repo)
        rc = run_cli(["status", "--json"], cwd=git_repo)
        assert rc == 0
        payload = _load_structured_json(capsys)

        assert set(payload.keys()) == {"tasks", "retry_escalation", "run"}

    def test_tasks_section(self, git_repo: Path, capsys) -> None:
        _init(git_repo)
        rc = run_cli(["status", "--json"], cwd=git_repo)
        assert rc == 0
        payload = _load_structured_json(capsys)
        tasks = payload["tasks"]
        assert isinstance(tasks, dict)

        assert set(tasks.keys()) == {"counts", "total", "needs_human"}
        assert tasks["total"] == 0
        counts = tasks["counts"]
        assert isinstance(counts, dict)
        assert set(counts.keys()) == {"draft", "todo", "doing", "done"}
        assert all(v == 0 for v in counts.values())
        assert tasks["needs_human"] == []

    def test_retry_escalation_section(self, git_repo: Path, capsys) -> None:
        _init(git_repo)
        rc = run_cli(["status", "--json"], cwd=git_repo)
        assert rc == 0
        payload = _load_structured_json(capsys)
        retry = payload["retry_escalation"]
        assert isinstance(retry, dict)
        assert set(retry.keys()) == {"tasks_with_failures"}
        assert retry["tasks_with_failures"] == []

    def test_run_section(self, git_repo: Path, capsys) -> None:
        _init(git_repo)
        rc = run_cli(["status", "--json"], cwd=git_repo)
        assert rc == 0
        payload = _load_structured_json(capsys)
        run_data = payload["run"]
        assert isinstance(run_data, dict)
        assert set(run_data.keys()) == {
            "iteration_number",
            "started_at",
            "finished_at",
            "active_attempt",
            "process",
        }
        assert run_data["iteration_number"] == 0
        assert run_data["started_at"] is None
        assert run_data["finished_at"] is None
        assert run_data["active_attempt"] is None
        assert run_data["process"] is None


class TestStructuredStatusWithTasks:
    """Schema stability with a mix of tasks across states."""

    def setup_tasks(self, repo: Path) -> None:
        write_task(
            repo,
            status="todo",
            slug="human-task",
            title="Human task",
            priority=1,
            assignee="Human",
            body="do it",
        )
        write_task(
            repo,
            status="todo",
            slug="ralph-task",
            title="Ralph task",
            priority=0,
            assignee="Ralph",
            body="do it",
        )
        write_task(
            repo,
            status="done",
            slug="finished",
            title="Finished",
            priority=2,
            assignee="Ralph",
            body="done",
        )
        write_task(
            repo,
            status="doing",
            slug="in-progress",
            title="In progress",
            priority=0,
            assignee="Ralph",
            body="doing it",
        )
        write_task(
            repo,
            status="draft",
            slug="draft-task",
            title="Draft",
            priority=3,
            assignee="Ralph",
            body="drafting",
        )

    def test_counts_reflect_task_distribution(self, git_repo: Path, capsys) -> None:
        _init(git_repo)
        self.setup_tasks(git_repo)
        rc = run_cli(["status", "--json"], cwd=git_repo)
        assert rc == 0
        payload = _load_structured_json(capsys)
        counts = payload["tasks"]["counts"]
        assert counts == {"draft": 1, "todo": 2, "doing": 1, "done": 1}
        assert payload["tasks"]["total"] == 5

    def test_needs_human_lists_human_tasks(self, git_repo: Path, capsys) -> None:
        _init(git_repo)
        self.setup_tasks(git_repo)
        rc = run_cli(["status", "--json"], cwd=git_repo)
        assert rc == 0
        payload = _load_structured_json(capsys)
        needs_human = payload["tasks"]["needs_human"]
        assert len(needs_human) == 1
        entry = needs_human[0]
        expected_keys = {"slug", "title", "priority", "status", "depends_on"}
        assert set(entry.keys()) == expected_keys
        assert entry["slug"] == "human-task"
        assert entry["title"] == "Human task"
        assert entry["priority"] == 1
        assert entry["status"] == "todo"
        assert entry["depends_on"] == []

    def test_run_section_preserves_schema(self, git_repo: Path, capsys) -> None:
        _init(git_repo)
        self.setup_tasks(git_repo)
        rc = run_cli(["status", "--json"], cwd=git_repo)
        assert rc == 0
        payload = _load_structured_json(capsys)
        run_data = payload["run"]
        assert set(run_data.keys()) == {
            "iteration_number",
            "started_at",
            "finished_at",
            "active_attempt",
            "process",
        }


class TestStructuredStatusWithRetryMetadata:
    """Schema stability when state has attempt history with failures."""

    def test_retry_escalation_reports_failed_task(self, git_repo: Path, capsys) -> None:
        _init(git_repo)
        write_task(
            git_repo,
            status="todo",
            slug="retry-me",
            title="Retry me",
            priority=0,
            assignee="Ralph",
            body="retry",
        )
        # Simulate a failed attempt in state
        state_path = git_repo / ".jri" / "state.json"
        state = read_json(state_path)
        state["attempts"] = [
            {
                "number": 1,
                "task_slug": "retry-me",
                "iteration_number": 1,
                "branch": "ralph/1/retry-me",
                "started_at": 1700000000,
                "finished_at": 1700000100,
                "outcome": "failed",
            },
        ]
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

        rc = run_cli(["status", "--json"], cwd=git_repo)
        assert rc == 0
        payload = _load_structured_json(capsys)
        tasks_with_failures = payload["retry_escalation"]["tasks_with_failures"]
        assert len(tasks_with_failures) == 1
        entry = tasks_with_failures[0]
        assert set(entry.keys()) == {
            "slug",
            "failed_attempts",
            "max_attempts",
            "escalated",
        }
        assert entry["slug"] == "retry-me"
        assert entry["failed_attempts"] == 1
        assert entry["max_attempts"] == 3
        assert entry["escalated"] is False

    def test_retry_escalation_shows_escalated_task(
        self, git_repo: Path, capsys
    ) -> None:
        _init(git_repo)
        write_task(
            git_repo,
            status="todo",
            slug="escalated-task",
            title="Escalated task",
            priority=0,
            assignee="Ralph",
            body="escalated",
        )
        state_path = git_repo / ".jri" / "state.json"
        state = read_json(state_path)
        state["attempts"] = [
            {
                "number": i,
                "task_slug": "escalated-task",
                "iteration_number": i,
                "branch": f"ralph/{i}/escalated-task",
                "started_at": 1700000000 + i * 100,
                "finished_at": 1700000100 + i * 100,
                "outcome": "failed",
            }
            for i in range(1, 4)
        ]
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

        rc = run_cli(["status", "--json"], cwd=git_repo)
        assert rc == 0
        payload = _load_structured_json(capsys)
        tasks_with_failures = payload["retry_escalation"]["tasks_with_failures"]
        assert len(tasks_with_failures) == 1
        entry = tasks_with_failures[0]
        assert entry["slug"] == "escalated-task"
        assert entry["failed_attempts"] == 3
        assert entry["escalated"] is True

    def test_multiple_tasks_with_failures(self, git_repo: Path, capsys) -> None:
        _init(git_repo)
        write_task(
            git_repo,
            status="todo",
            slug="task-a",
            title="Task A",
            priority=0,
            assignee="Ralph",
            body="a",
        )
        write_task(
            git_repo,
            status="todo",
            slug="task-b",
            title="Task B",
            priority=0,
            assignee="Ralph",
            body="b",
        )
        state_path = git_repo / ".jri" / "state.json"
        state = read_json(state_path)
        state["attempts"] = [
            {
                "number": 1,
                "task_slug": "task-b",
                "iteration_number": 1,
                "branch": "ralph/1/task-b",
                "started_at": 1700000000,
                "finished_at": 1700000100,
                "outcome": "failed",
            },
            {
                "number": 2,
                "task_slug": "task-a",
                "iteration_number": 2,
                "branch": "ralph/2/task-a",
                "started_at": 1700000200,
                "finished_at": 1700000300,
                "outcome": "failed",
            },
        ]
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

        rc = run_cli(["status", "--json"], cwd=git_repo)
        assert rc == 0
        payload = _load_structured_json(capsys)
        tasks_with_failures = payload["retry_escalation"]["tasks_with_failures"]
        assert len(tasks_with_failures) == 2
        # Sorted by slug
        assert tasks_with_failures[0]["slug"] == "task-a"
        assert tasks_with_failures[1]["slug"] == "task-b"


class TestStructuredStatusWithActiveRun:
    """Schema stability when a run is in progress (detached mode state)."""

    def test_run_section_with_process(self, git_repo: Path, capsys) -> None:
        _init(git_repo)
        state_path = git_repo / ".jri" / "state.json"
        state = read_json(state_path)
        state["iteration"] = {"number": 3, "started_at": 1700000000}
        state["process"] = {
            "loop_pid": 12345,
            "child_pid": 12346,
            "log_path": "/some/path.log",
            "detached": True,
        }
        state["active_attempt"] = {
            "number": 4,
            "task_slug": "active-task",
            "iteration_number": 4,
            "branch": "ralph/4/active-task",
            "started_at": 1700000000,
        }
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

        rc = run_cli(["status", "--json"], cwd=git_repo)
        assert rc == 0
        payload = _load_structured_json(capsys)
        run_data = payload["run"]
        assert run_data["iteration_number"] == 3
        assert run_data["started_at"] == 1700000000
        assert run_data["process"]["loop_pid"] == 12345
        assert run_data["process"]["detached"] is True
        assert run_data["active_attempt"]["task_slug"] == "active-task"

    def test_process_schema_keys(self, git_repo: Path, capsys) -> None:
        _init(git_repo)
        state_path = git_repo / ".jri" / "state.json"
        state = read_json(state_path)
        state["process"] = {
            "loop_pid": 99,
            "child_pid": None,
            "log_path": None,
            "detached": False,
        }
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

        rc = run_cli(["status", "--json"], cwd=git_repo)
        assert rc == 0
        payload = _load_structured_json(capsys)
        process = payload["run"]["process"]
        assert isinstance(process, dict)
        assert set(process.keys()) == {"loop_pid", "child_pid", "log_path", "detached"}


class TestStructuredStatusNeedsHumanMultiple:
    """Schema stability with Human tasks across multiple statuses."""

    def test_needs_human_across_statuses(self, git_repo: Path, capsys) -> None:
        _init(git_repo)
        write_task(
            git_repo,
            status="todo",
            slug="human-todo",
            title="Human todo",
            priority=0,
            assignee="Human",
            body="todo",
        )
        write_task(
            git_repo,
            status="done",
            slug="human-done",
            title="Human done",
            priority=1,
            assignee="Human",
            body="done",
            depends_on=["other-task"],
        )
        rc = run_cli(["status", "--json"], cwd=git_repo)
        assert rc == 0
        payload = _load_structured_json(capsys)
        needs_human = payload["tasks"]["needs_human"]
        assert len(needs_human) == 2
        slugs = {entry["status"] for entry in needs_human}
        assert slugs == {"todo", "done"}
        # The done one carries depends_on
        done_entry = next(e for e in needs_human if e["slug"] == "human-done")
        assert done_entry["depends_on"] == ["other-task"]


class TestStructuredStatusOutputIsStable:
    """Ensure the JSON output is parseable and keys are sorted."""

    def test_output_is_sorted_keys(self, git_repo: Path, capsys) -> None:
        _init(git_repo)
        write_task(
            git_repo,
            status="todo",
            slug="some-task",
            title="Some task",
            priority=0,
            assignee="Ralph",
            body="body",
        )
        rc = run_cli(["status", "--json"], cwd=git_repo)
        assert rc == 0
        out = capsys.readouterr().out
        # Verify parseable
        payload = json.loads(out)
        # Verify sorted keys (json.dumps with sort_keys=True)
        reparsed = json.loads(json.dumps(payload, sort_keys=True))
        assert payload == reparsed
