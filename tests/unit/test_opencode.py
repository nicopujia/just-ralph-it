import json

import pytest

from jri.core.opencode import _parse_event_line


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

    event, terminal_text = _parse_event_line(f"{line}\n")

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

    event, terminal_text = _parse_event_line(f"{line}\n")

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

    event, terminal_text = _parse_event_line(f"{line}\n")

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

    event, terminal_text = _parse_event_line(f"{line}\n")

    assert event == {
        "type": "step_start",
        "sessionID": "ses_123",
        "part": {
            "type": "step-start",
        },
    }
    assert terminal_text is None


def test_parse_event_line_preserves_plain_text_fallback() -> None:
    event, terminal_text = _parse_event_line("plain text fallback\n")

    assert event is None
    assert terminal_text == "plain text fallback\n"


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

    assert _finalize_outcome(None, context="Ralph run") == "failed"
    assert (
        "missing JRI outcome marker for Ralph run; treating run as failed"
        in capsys.readouterr().err
    )
