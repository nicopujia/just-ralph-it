import json
from pathlib import Path
from typing import cast

import pytest

from jri.core.errors import JriError
from jri.core.models import AgentRunResult, HumanTaskPayload, RalphResultPayload
from jri.core.service import JriService
from jri.core.tasks import list_tasks, parse_task_file
from tests.conftest import run_cli
from tests.helpers import git, read_json, write_passing_makefile, write_task

pytestmark = pytest.mark.e2e


class DogfoodFakeAgentRuntime:
    def __init__(self) -> None:
        self.model: str | None = None
        self.calls: list[str] = []
        self._slug_calls: dict[str, int] = {}

    def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
        del root, limit
        return []

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: object | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        del result_path, on_start, timeout
        slug = _extract_task_slug(prompt)
        self.calls.append(slug)
        self._slug_calls[slug] = self._slug_calls.get(slug, 0) + 1

        if slug == "complete-first":
            (root / "complete-first.txt").write_text(
                "complete-first ok\n", encoding="utf-8"
            )
            log_path.write_text("completed complete-first\n", encoding="utf-8")
            return AgentRunResult(
                returncode=0,
                session_id="ses_complete_first",
                result="completed",
                payload=RalphResultPayload(
                    result="completed",
                    summary="Completed the first deterministic dogfood task.",
                ),
            )

        if slug == "needs-human-origin" and self._slug_calls[slug] == 1:
            log_path.write_text(
                "needs human for deterministic input\n", encoding="utf-8"
            )
            return AgentRunResult(
                returncode=0,
                session_id="ses_needs_human_origin",
                result="needs_human",
                payload=RalphResultPayload(
                    result="needs_human",
                    blocker="A deterministic human approval is required.",
                    human_task=HumanTaskPayload(
                        title="Approve deterministic dogfood input",
                        body=(
                            "Confirm the synthetic dogfood-derived input is available."
                        ),
                        acceptance_criteria=["The synthetic input is approved"],
                    ),
                ),
            )

        if slug == "needs-human-origin":
            (root / "needs-human-origin.txt").write_text(
                "needs-human-origin resumed\n",
                encoding="utf-8",
            )
            log_path.write_text("resumed after human input\n", encoding="utf-8")
            return AgentRunResult(
                returncode=0,
                session_id="ses_needs_human_resumed",
                result="completed",
                payload=RalphResultPayload(
                    result="completed",
                    summary="Resumed successfully after explicit Human completion.",
                ),
            )

        if slug == "startup-before-payload":
            raise JriError("provider startup failed before payload or provider log")

        raise AssertionError(f"unexpected task slug: {slug}")

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.write_text(
            json.dumps({"session": session_id, "source": "deterministic-fake"}) + "\n",
            encoding="utf-8",
        )


def test_dogfood_jri_lifecycle_regression(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(["init"], cwd=git_repo) == 0
    write_passing_makefile(git_repo)
    git(git_repo, "add", "Makefile")
    git(git_repo, "commit", "-m", "configure deterministic check")
    _write_dogfood_tasks(git_repo)

    runtime = DogfoodFakeAgentRuntime()
    service = JriService(git_repo, agent_runtime=runtime)

    first_summary = service.start_summary(max_tasks=3, force=True)

    assert first_summary.completed == 1
    assert first_summary.outcome == "task_failure"
    assert first_summary.task_results == {
        "complete-first": "completed",
        "needs-human-origin": "needs_human",
        "startup-before-payload": "failed",
    }
    assert runtime.calls == [
        "complete-first",
        "needs-human-origin",
        "startup-before-payload",
    ]
    assert (git_repo / ".jri" / "tasks" / "done" / "complete-first.md").exists()
    assert (git_repo / "complete-first.txt").read_text(encoding="utf-8") == (
        "complete-first ok\n"
    )

    todo_tasks = list_tasks(git_repo / ".jri" / "tasks" / "todo")
    human_tasks = [task for task in todo_tasks if task.metadata.assignee == "Human"]
    assert [task.slug for task in human_tasks] == ["needs-human-origin--needs-human"]
    original = parse_task_file(
        git_repo / ".jri" / "tasks" / "todo" / "needs-human-origin.md"
    )
    assert original.metadata.depends_on == ["needs-human-origin--needs-human"]
    assert (git_repo / ".jri" / "tasks" / "todo" / "startup-before-payload.md").exists()

    state = read_json(git_repo / ".jri" / "state.json")
    assert state.get("active_attempt") is None
    attempts = cast(list[dict[str, object]], state["attempts"])
    assert [(attempt["task_slug"], attempt["result"]) for attempt in attempts] == [
        ("complete-first", "completed"),
        ("needs-human-origin", "needs_human"),
        ("startup-before-payload", "failed"),
    ]
    startup_attempt = attempts[2]
    startup_log_path = Path(cast(str, startup_attempt["log_path"]))
    startup_log = startup_log_path.read_text(encoding="utf-8")
    assert "provider startup failed before payload or provider log" in startup_log
    assert _text_has_no_secret_markers(startup_log)

    capsys.readouterr()
    assert run_cli(["status"], cwd=git_repo) == 0
    status_output = capsys.readouterr().out
    assert "Tasks: 4 total" in status_output
    assert (
        "Action needed: complete Human task needs-human-origin--needs-human, "
        "then run `jri complete-human needs-human-origin--needs-human`."
    ) in status_output

    service.inspect("startup-before-payload")
    inspect_output = capsys.readouterr().out
    assert "startup-before-payload" in inspect_output
    assert "provider startup failed before payload or provider log" in inspect_output
    assert "failed" in inspect_output
    assert _text_has_no_secret_markers(inspect_output)

    assert run_cli(["timeline"], cwd=git_repo) == 0
    timeline_output = capsys.readouterr().out
    assert "task_completed task=complete-first" in timeline_output
    assert "task_needs_human task=needs-human-origin" in timeline_output
    assert "agent_runtime_exception" in timeline_output
    assert "recovery_completed task=startup-before-payload" in timeline_output
    assert _text_has_no_secret_markers(timeline_output)

    assert (
        run_cli(["complete-human", "needs-human-origin--needs-human"], cwd=git_repo)
        == 0
    )
    capsys.readouterr()
    assert (
        git_repo / ".jri" / "tasks" / "done" / "needs-human-origin--needs-human.md"
    ).exists()
    assert not (git_repo / ".jri" / "tasks" / "done" / "needs-human-origin.md").exists()

    assert run_cli(["status"], cwd=git_repo) == 0
    unblocked_status = capsys.readouterr().out
    assert "[done  ] [P1] needs-human-origin--needs-human" in unblocked_status
    assert (
        "Action needed: run `jri start` to retry needs-human-origin."
        in unblocked_status
    )

    retry_summary = JriService(git_repo, agent_runtime=runtime).start_summary(
        max_tasks=1,
        force=True,
    )

    assert retry_summary.completed == 1
    assert retry_summary.outcome == "completed"
    assert retry_summary.task_results == {"needs-human-origin": "completed"}
    assert runtime.calls == [
        "complete-first",
        "needs-human-origin",
        "startup-before-payload",
        "needs-human-origin",
    ]
    assert (git_repo / ".jri" / "tasks" / "done" / "needs-human-origin.md").exists()
    assert (git_repo / "needs-human-origin.txt").read_text(encoding="utf-8") == (
        "needs-human-origin resumed\n"
    )
    assert (git_repo / ".jri" / "tasks" / "todo" / "startup-before-payload.md").exists()

    capsys.readouterr()
    assert run_cli(["status"], cwd=git_repo) == 0
    final_status = capsys.readouterr().out
    assert "Tasks: 4 total" in final_status
    assert (
        "Action needed: run `jri start` to retry startup-before-payload."
        in final_status
    )

    assert run_cli(["timeline", "--json"], cwd=git_repo) == 0
    timeline_events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert _event_names(timeline_events, "complete-first") >= {
        "attempt_started",
        "make_check_passed",
        "task_completed",
    }
    assert _event_names(timeline_events, "needs-human-origin") >= {
        "attempt_started",
        "task_needs_human",
        "task_completed",
    }
    assert _event_names(timeline_events, "startup-before-payload") >= {
        "attempt_started",
        "stderr_warning",
        "task_failed",
        "recovery_completed",
    }
    assert _event_names(timeline_events, "needs-human-origin--needs-human") == {
        "human_task_completed"
    }


def _write_dogfood_tasks(repo: Path) -> None:
    write_task(
        repo,
        status="todo",
        slug="complete-first",
        title="Complete first deterministic dogfood task",
        priority=0,
        assignee="Ralph",
        body="Create complete-first.txt with deterministic content.",
        acceptance_criteria=["complete-first.txt contains deterministic content"],
    )
    write_task(
        repo,
        status="todo",
        slug="needs-human-origin",
        title="Escalate and resume deterministic human blocker",
        priority=1,
        assignee="Ralph",
        body="Ask for deterministic human input, then resume after it is complete.",
        acceptance_criteria=["needs-human-origin.txt exists after retry"],
    )
    write_task(
        repo,
        status="todo",
        slug="startup-before-payload",
        title="Recover startup failure before payload",
        priority=2,
        assignee="Ralph",
        body="The runtime fails before producing a structured payload or provider log.",
        acceptance_criteria=["The failed attempt is recovered and inspectable"],
    )
    git(repo, "add", ".jri/tasks/todo")
    git(repo, "commit", "-m", "add deterministic dogfood tasks")


def _extract_task_slug(prompt: str) -> str:
    marker = ".jri/tasks/doing/"
    start = prompt.index(marker) + len(marker)
    end = prompt.index(".md", start)
    return prompt[start:end]


def _event_names(events: list[dict[str, object]], task: str) -> set[str]:
    return {cast(str, event["event"]) for event in events if event.get("task") == task}


def _text_has_no_secret_markers(text: str) -> bool:
    lowered = text.lower()
    forbidden = (
        "api_key",
        "apikey",
        "authorization:",
        "bearer ",
        "certificate",
        "credential",
        "private key",
        "secret=",
        "token=",
    )
    return not any(marker in lowered for marker in forbidden)
