import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from jri.core.agents.bundle._shared import tools
from jri.core.agents.client import (
    SavedLogRenderer,
    _parse_event_line,
    _parse_result_payload,
    launch_chat,
    render_saved_event,
)
from jri.core.errors import JriError


def invoke_tool(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    payload: object,
) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return tools.main([tool_name])


def valid_task_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": "Cover final branch",
        "body": "Exercise meaningful validation behavior.",
        "assignee": "Ralph",
        "priority": 1,
        "depends_on": [],
        "acceptance_criteria": ["The branch is covered"],
    }
    payload.update(overrides)
    return payload


def write_todo_task(repo: Path, slug: str, body: str = "Original body.\n") -> Path:
    task_path = repo / ".jri" / "tasks" / "todo" / f"{slug}.md"
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(
        "---\n"
        + json.dumps(
            {
                "title": slug.replace("-", " ").title(),
                "priority": 1,
                "assignee": "Ralph",
                "depends_on": [],
                "acceptance_criteria": ["observable"],
            }
        )
        + "\n---\n\n"
        + body,
        encoding="utf-8",
    )
    return task_path


def test_saved_log_renderer_tracks_task_tools_and_filters_duplicate_events() -> None:
    renderer = SavedLogRenderer(cwd_hint="/repo/")
    seen: set[str] = set()

    assert render_saved_event(
        {"payload": {"type": "message_update", "text": "hi"}}, seen_tool_calls=seen
    ) == ("hi", False)
    assert renderer.render_chunk("plain line\n") == "plain line\n"

    first_tool = cast(
        dict[str, object],
        {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "type": "tool",
                    "tool": "task",
                    "callID": "call-1",
                    "state": {
                        "status": "running",
                        "input": {"description": "inspect repo"},
                    },
                }
            },
        },
    )
    text, newline_before = renderer.render_event(first_tool)
    assert newline_before is True
    assert "task inspect repo" in text
    assert renderer.active_task_detail == "inspect repo"

    assert renderer.render_event(first_tool) == ("", False)
    assert renderer.render_event({"type": "message_end"}) == ("\n", False)

    start_event = cast(
        dict[str, object],
        {
            "type": "tool_execution_start",
            "toolName": "read",
            "toolCallId": "call-2",
            "input": {"filePath": "/repo/src/module.py"},
        },
    )
    text, newline_before = renderer.render_event(start_event)
    assert newline_before is True
    assert "read src/module.py" in text

    assert renderer.render_event(
        {"type": "tool_execution_update", "toolCallId": "call-2"}
    ) == ("", False)
    assert (
        renderer.render_chunk(
            json.dumps(
                {
                    "type": "message.part.delta",
                    "properties": {"field": "text", "delta": "done"},
                }
            ),
            final=True,
        )
        == "done"
    )


def test_parse_event_line_and_result_payload_validation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _parse_event_line("not-json") == (None, "not-json", False)
    assert _parse_event_line("[]") == (None, None, False)
    assert _parse_event_line(
        json.dumps({"type": "message_update", "delta": "hello"})
    ) == (
        {"type": "message_update", "delta": "hello"},
        "hello",
        False,
    )
    event, text, is_tool_output = _parse_event_line(
        json.dumps({"type": "tool_execution_end", "output": "x" * 4000})
    )
    assert event == {"type": "tool_execution_end", "output": "x" * 4000}
    assert text is not None and "trimmed" in text
    assert is_tool_output is True

    assert _parse_result_payload("[")[0] is None
    assert "invalid result payload" in capsys.readouterr().err
    assert _parse_result_payload("[]")[0] is None
    assert "expected object" in capsys.readouterr().err
    assert _parse_result_payload(json.dumps({"result": "unknown"}))[0] is None
    assert "missing or unknown" in capsys.readouterr().err
    assert _parse_result_payload(json.dumps({"result": "incompleted"}))[0] is None
    assert "requires non-empty" in capsys.readouterr().err
    assert (
        _parse_result_payload(
            json.dumps(
                {
                    "result": "needs_human",
                    "blocker": "decision needed",
                    "human_task": {
                        "slug": "not-accepted",
                        "title": "Choose path",
                        "body": "Pick one.",
                        "acceptance_criteria": ["choice exists"],
                    },
                }
            )
        )[0]
        is None
    )
    assert "slug` is not supported" in capsys.readouterr().err

    payload, warnings = _parse_result_payload(
        json.dumps(
            {
                "result": "needs_human",
                "blocker": "decision needed",
                "human_task": {
                    "title": "Choose path",
                    "body": "Pick one.",
                    "acceptance_criteria": ["choice exists"],
                    "priority": 2,
                },
            }
        )
    )
    assert warnings == []
    assert payload is not None
    assert payload.result == "needs_human"
    assert payload.human_task is not None
    assert payload.human_task.priority == 2


def test_launch_chat_builds_managed_capability_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "package"
    (package_root / "interrogator").mkdir(parents=True)
    (package_root / "ralph" / "skills" / "alpha").mkdir(parents=True)
    (package_root / "ralph" / "skills" / "file.txt").write_text(
        "ignored", encoding="utf-8"
    )
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, cwd, env))
        assert check is False
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr("jri.core.agents.client.subprocess.run", fake_run)

    assert (
        launch_chat(
            root=tmp_path,
            session_id="ses_123",
            extra_args=["--model", "fake"],
            binary="pi-fake",
            env={"JRI_PI_PACKAGE": str(package_root), "EXTRA": "1"},
            session_dir=tmp_path / "sessions",
        )
        == 7
    )

    command, cwd, env = calls[0]
    assert cwd == tmp_path
    assert env["JRI_CHAT_RUNTIME"] == "1"
    assert env["EXTRA"] == "1"
    assert command[:5] == [
        "pi-fake",
        "--session-dir",
        str(tmp_path / "sessions"),
        "--session",
        "ses_123",
    ]
    assert "--no-extensions" in command
    assert "--extension" in command
    assert str(package_root / "extension.ts") in command
    assert str(package_root / "interrogator" / "prompt.md") in command

    with pytest.raises(JriError, match="unsupported arg: --tools"):
        launch_chat(
            root=tmp_path,
            session_id=None,
            extra_args=["--tools=read"],
            binary="pi-fake",
        )


def test_tool_upsert_task_rejects_invalid_metadata_and_symlinked_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    assert (
        invoke_tool(monkeypatch, "upsert-task", valid_task_payload(title="x" * 76)) == 1
    )
    assert "75 characters" in capsys.readouterr().err
    assert (
        invoke_tool(monkeypatch, "upsert-task", valid_task_payload(assignee="Bot")) == 1
    )
    assert "Ralph" in capsys.readouterr().err
    assert (
        invoke_tool(monkeypatch, "upsert-task", valid_task_payload(priority=True)) == 1
    )
    assert "integer from 0 to 4" in capsys.readouterr().err
    assert (
        invoke_tool(
            monkeypatch, "upsert-task", valid_task_payload(depends_on=["dup", "dup"])
        )
        == 1
    )
    assert "must not contain duplicates" in capsys.readouterr().err
    assert invoke_tool(monkeypatch, "upsert-task", valid_task_payload(title="!!!")) == 1
    assert "could not derive" in capsys.readouterr().err

    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    todo_dir = tmp_path / ".jri" / "tasks" / "todo"
    todo_dir.mkdir(parents=True, exist_ok=True)
    (todo_dir / "linked.md").symlink_to(outside)
    assert (
        invoke_tool(monkeypatch, "upsert-task", valid_task_payload(slug="linked")) == 1
    )
    assert "refusing to write outside `.jri/tasks/`" in capsys.readouterr().err


def test_tool_task_writes_reject_existing_todo_conflicts_and_removed_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    write_todo_task(tmp_path, "source")

    assert (
        invoke_tool(monkeypatch, "upsert-task", valid_task_payload(slug="source")) == 1
    )
    assert "refusing to overwrite existing todo task" in capsys.readouterr().err

    for removed_tool in ("rename-task", "delete-task", "edit-draft-task"):
        assert invoke_tool(monkeypatch, removed_tool, {"slug": "source"}) == 2
        assert "expected one tool name" in capsys.readouterr().err


def test_tool_list_and_read_tasks_validate_empty_and_filtered_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeService:
        def __init__(self, root: Path) -> None:
            self.paths = SimpleNamespace(tasks_dir=root / ".jri" / "tasks")
            self.git = None

        def status(self) -> dict[str, list[object]]:
            return {"todo": [], "doing": [], "done": []}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tools, "JriService", FakeService)

    assert invoke_tool(monkeypatch, "read-tasks", {"slugs": []}) == 1
    assert "non-empty list" in capsys.readouterr().err
    assert invoke_tool(monkeypatch, "list-tasks", {"status": "blocked"}) == 1
    assert "todo, doing, done" in capsys.readouterr().err
