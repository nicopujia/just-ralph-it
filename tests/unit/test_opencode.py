import json
from pathlib import Path
from urllib.error import URLError

import pytest

from jri.core.errors import JriError
from jri.core.opencode import OpenCodeServer, _parse_event_line


def test_parse_event_line_extracts_terminal_text_from_text_event() -> None:
    line = json.dumps(
        {
            "type": "text",
            "sessionID": "ses_123",
            "part": {
                "type": "text",
                "text": "hello from opencode",
            },
        }
    )

    event, terminal_text, is_tool = _parse_event_line(f"{line}\n")

    assert event == {
        "type": "text",
        "sessionID": "ses_123",
        "part": {
            "type": "text",
            "text": "hello from opencode",
        },
    }
    assert terminal_text == "hello from opencode"


def test_parse_event_line_extracts_tool_output() -> None:
    line = json.dumps(
        {
            "type": "tool_use",
            "sessionID": "ses_123",
            "part": {
                "type": "tool",
                "tool": "read",
                "state": {
                    "output": "README.md\nsrc\n",
                },
            },
        }
    )

    event, terminal_text, is_tool = _parse_event_line(f"{line}\n")

    assert event == {
        "type": "tool_use",
        "sessionID": "ses_123",
        "part": {
            "type": "tool",
            "tool": "read",
            "state": {
                "output": "README.md\nsrc\n",
            },
        },
    }
    assert terminal_text == "README.md\nsrc\n"


def test_parse_event_line_prefers_tool_error_over_output() -> None:
    line = json.dumps(
        {
            "type": "tool_use",
            "sessionID": "ses_123",
            "part": {
                "type": "tool",
                "tool": "read",
                "state": {
                    "error": "File not found",
                    "output": "ignored output",
                },
            },
        }
    )

    event, terminal_text, is_tool = _parse_event_line(f"{line}\n")

    assert event == {
        "type": "tool_use",
        "sessionID": "ses_123",
        "part": {
            "type": "tool",
            "tool": "read",
            "state": {
                "error": "File not found",
                "output": "ignored output",
            },
        },
    }
    assert terminal_text == "File not found"


def test_parse_event_line_suppresses_non_display_json_events() -> None:
    line = json.dumps(
        {
            "type": "step_start",
            "sessionID": "ses_123",
            "part": {
                "type": "step-start",
            },
        }
    )

    event, terminal_text, is_tool = _parse_event_line(f"{line}\n")

    assert event == {
        "type": "step_start",
        "sessionID": "ses_123",
        "part": {
            "type": "step-start",
        },
    }
    assert terminal_text is None


def test_parse_event_line_preserves_plain_text_fallback() -> None:
    event, terminal_text, is_tool = _parse_event_line("plain text fallback\n")

    assert event is None
    assert terminal_text == "plain text fallback\n"


def test_parse_event_line_returns_plain_text_for_malformed_json() -> None:
    event, terminal_text, is_tool = _parse_event_line("{not json}\n")

    assert event is None
    assert terminal_text == "{not json}\n"
    assert is_tool is False


def test_detect_outcome_completed() -> None:
    from jri.core.opencode import _detect_outcome

    assert _detect_outcome("<!-- JRI:COMPLETED -->", None) == "completed"


def test_detect_outcome_failed() -> None:
    from jri.core.opencode import _detect_outcome

    assert _detect_outcome("<!-- JRI:FAILED -->", None) == "failed"


def test_detect_outcome_needs_human() -> None:
    from jri.core.opencode import _detect_outcome

    assert _detect_outcome("<!-- JRI:NEEDS_HUMAN -->", None) == "needs human"


def test_detect_outcome_no_marker_preserves_current() -> None:
    from jri.core.opencode import _detect_outcome

    assert _detect_outcome("just some text", None) is None
    assert _detect_outcome("just some text", "completed") == "completed"


def test_detect_outcome_embedded_in_text() -> None:
    from jri.core.opencode import _detect_outcome

    text = "preamble <!-- JRI:COMPLETED --> trailing"

    assert _detect_outcome(text, None) == "completed"


def test_detect_outcome_uses_last_marker_in_text() -> None:
    from jri.core.opencode import _detect_outcome

    assert (
        _detect_outcome(
            "<!-- JRI:COMPLETED --> then <!-- JRI:FAILED -->",
            None,
        )
        == "failed"
    )


def test_finalize_outcome_missing_marker_treats_run_as_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jri.core.opencode import _finalize_outcome

    outcome, warnings = _finalize_outcome(None, context="Ralph run")
    assert outcome == "failed"
    msg = "missing JRI outcome marker for Ralph run; treating run as failed"
    assert warnings == [msg]
    assert msg in capsys.readouterr().err


def test_parse_event_line_returns_is_tool_false_for_text_event() -> None:
    line = json.dumps(
        {
            "type": "text",
            "sessionID": "ses_123",
            "part": {
                "type": "text",
                "text": "hello",
            },
        }
    )

    _, _, is_tool = _parse_event_line(f"{line}\n")

    assert is_tool is False


def test_parse_event_line_returns_is_tool_true_for_tool_use_event() -> None:
    line = json.dumps(
        {
            "type": "tool_use",
            "sessionID": "ses_123",
            "part": {
                "type": "tool",
                "tool": "read",
                "state": {
                    "output": "some output",
                },
            },
        }
    )

    _, _, is_tool = _parse_event_line(f"{line}\n")

    assert is_tool is True


def test_parse_event_line_returns_is_tool_false_for_plain_text() -> None:
    _, _, is_tool = _parse_event_line("plain text fallback\n")

    assert is_tool is False


def test_parse_event_line_returns_is_tool_false_for_non_display_json() -> None:
    line = json.dumps(
        {
            "type": "step_start",
            "sessionID": "ses_123",
            "part": {
                "type": "step-start",
            },
        }
    )

    _, _, is_tool = _parse_event_line(f"{line}\n")

    assert is_tool is False


def test_run_ralph_task_raises_on_session_create_http_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(binary="opencode")

    def fake_http_request(
        method: str,
        url: str,
        *,
        body: object | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, bytes]:
        return 500, b"boom"

    monkeypatch.setattr("jri.core.opencode._http_request", fake_http_request)

    with pytest.raises(
        JriError, match=r"failed to create opencode session \(HTTP 500\): boom"
    ):
        server.run_ralph_task(
            root=tmp_path,
            prompt="Solve the task",
            log_path=tmp_path / "ralph.log",
            outcome_path=tmp_path / "result.txt",
        )


def test_run_ralph_task_cleans_up_session_on_prompt_http_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(binary="opencode")
    deleted: list[str] = []
    calls: list[tuple[str, str]] = []

    def fake_http_request(
        method: str,
        url: str,
        *,
        body: object | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, bytes]:
        calls.append((method, url))
        if len(calls) == 1:
            return 201, b'{"id": "ses_123"}'
        return 500, b"prompt failed"

    def fake_urlopen(*args: object, **kwargs: object) -> object:
        raise URLError("offline")

    def fake_delete_session(session_id: str) -> None:
        deleted.append(session_id)

    monkeypatch.setattr("jri.core.opencode._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(server, "_delete_session", fake_delete_session)

    with pytest.raises(JriError, match="failed to start ralph prompt"):
        server.run_ralph_task(
            root=tmp_path,
            prompt="Solve the task",
            log_path=tmp_path / "ralph.log",
            outcome_path=tmp_path / "result.txt",
        )

    assert calls[0][0] == "POST"
    assert "/session?directory=" in calls[0][1]
    assert deleted == ["ses_123"]


def test_parse_event_line_trims_long_tool_output() -> None:
    long_output = "\n".join(f"line {i}" for i in range(30))
    line = json.dumps(
        {
            "type": "tool_use",
            "sessionID": "ses_123",
            "part": {
                "type": "tool",
                "tool": "read",
                "state": {
                    "output": long_output,
                },
            },
        }
    )

    _, terminal_text, is_tool = _parse_event_line(f"{line}\n")

    assert is_tool is True
    assert terminal_text is not None
    assert "lines trimmed" in terminal_text


def test_parse_event_line_preserves_short_tool_output() -> None:
    short_output = "line 0\nline 1\nline 2"
    line = json.dumps(
        {
            "type": "tool_use",
            "sessionID": "ses_123",
            "part": {
                "type": "tool",
                "tool": "read",
                "state": {
                    "output": short_output,
                },
            },
        }
    )

    _, terminal_text, is_tool = _parse_event_line(f"{line}\n")

    assert is_tool is True
    assert terminal_text == short_output
