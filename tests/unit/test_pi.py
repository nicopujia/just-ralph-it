import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from jri.core.agents import (
    PiRuntime,
    _missing_result_payload,
    _parse_event_line,
    _parse_result_payload,
    render_saved_log,
)
from jri.core.errors import JriError


def _result_payload(result: str = "completed") -> str:
    return json.dumps({"result": result}) + "\n"


def test_render_saved_log_replays_streamed_text_and_tool_labels() -> None:
    text = "\n".join(
        [
            json.dumps({"type": "message_update", "delta": "hello"}),
            json.dumps(
                {
                    "type": "tool_execution_start",
                    "toolCallId": "call_1",
                    "toolName": "read",
                    "input": {"path": "/repo/file.txt"},
                }
            ),
            json.dumps({"type": "message_update", "delta": "done"}),
        ]
    )

    rendered = render_saved_log(text, cwd_hint="/repo/")

    assert "hello" in rendered
    assert "read file.txt" in rendered
    assert "done" in rendered


def test_parse_event_line_extracts_terminal_text_from_message_update() -> None:
    _, terminal_text, is_tool = _parse_event_line(
        json.dumps({"type": "message_update", "delta": "hello"}) + "\n"
    )

    assert terminal_text == "hello"
    assert is_tool is False


def test_parse_event_line_extracts_tool_output() -> None:
    _, terminal_text, is_tool = _parse_event_line(
        json.dumps(
            {
                "type": "tool_execution_end",
                "toolName": "read",
                "output": "line 1\nline 2",
            }
        )
        + "\n"
    )

    assert terminal_text == "line 1\nline 2"
    assert is_tool is True


def test_parse_event_line_preserves_plain_text_fallback() -> None:
    payload, terminal_text, is_tool = _parse_event_line("plain text\n")

    assert payload is None
    assert terminal_text == "plain text\n"
    assert is_tool is False


def test_missing_result_payload_reports_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, warnings = _missing_result_payload(context="Ralph run")

    assert result == "failed"
    assert warnings == ["missing result payload for Ralph run; treating run as failed"]
    assert warnings[0] in capsys.readouterr().err


def test_parse_result_payload_accepts_completed() -> None:
    payload, warnings = _parse_result_payload(_result_payload("completed"))

    assert warnings == []
    assert payload is not None
    assert payload.result == "completed"


def test_parse_result_payload_validates_needs_human() -> None:
    payload, warnings = _parse_result_payload(
        json.dumps(
            {
                "result": "needs_human",
                "blocker": "missing secret",
                "human_task": {
                    "title": "Provide secret",
                    "body": "Add the production secret.",
                    "acceptance_criteria": ["Secret is available"],
                },
            }
        )
    )

    assert warnings == []
    assert payload is not None
    assert payload.result == "needs_human"
    assert payload.human_task is not None


def test_parse_result_payload_rejects_human_task_slug() -> None:
    payload, warnings = _parse_result_payload(
        json.dumps(
            {
                "result": "needs_human",
                "blocker": "missing secret",
                "human_task": {
                    "slug": "provide-secret",
                    "title": "Provide secret",
                    "body": "Add the production secret.",
                    "acceptance_criteria": ["Secret is available"],
                },
            }
        )
    )

    assert payload is None
    assert warnings == [
        "invalid result payload; treating run as failed: "
        "`human_task.slug` is not supported; JRI derives the Human task slug"
    ]


def test_pi_runtime_rpc_request_reads_matching_response() -> None:
    runtime = PiRuntime(binary="pi")
    stdin = io.StringIO()
    stdout = io.StringIO('{"type":"response","command":"get_state","success":true}\n')

    process = SimpleNamespace(
        pid=123,
        stdin=stdin,
        stdout=stdout,
        poll=lambda: None,
    )
    runtime._process = cast(Any, process)

    response = runtime._rpc_request("get_state")

    assert response["success"] is True
    assert json.loads(stdin.getvalue()) == {"type": "get_state"}


def test_pi_runtime_export_session_copies_session_file(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    session_file.write_text('{"type":"message"}\n', encoding="utf-8")
    destination = tmp_path / "export.json"
    runtime = PiRuntime(binary="pi")
    runtime._session_id = "ses_123"
    runtime._session_file = session_file

    runtime.export_session("ses_123", destination)

    assert destination.read_text(encoding="utf-8") == '{"type":"message"}\n'


def test_pi_runtime_export_session_rejects_unknown_session(tmp_path: Path) -> None:
    runtime = PiRuntime(binary="pi")
    runtime._session_id = "ses_123"
    runtime._session_file = tmp_path / "session.jsonl"

    with pytest.raises(JriError, match="unknown pi session"):
        runtime.export_session("ses_other", tmp_path / "export.json")
