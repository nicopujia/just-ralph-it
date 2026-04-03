import json

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


def test_parse_outcome_completed() -> None:
    from jri.core.opencode import parse_outcome

    lines = [
        _text_event("working on it..."),
        _text_event("<!-- JRI:COMPLETED -->"),
    ]
    assert parse_outcome(lines) == "completed"


def test_parse_outcome_blocked() -> None:
    from jri.core.opencode import parse_outcome

    lines = [
        _text_event("hit a blocker"),
        _text_event("<!-- JRI:BLOCKED -->"),
    ]
    assert parse_outcome(lines) == "blocked"


def test_parse_outcome_unknown_when_no_signal() -> None:
    from jri.core.opencode import parse_outcome

    lines = [
        _text_event("did some work"),
        _text_event("all done"),
    ]
    assert parse_outcome(lines) == "unknown"


def test_parse_outcome_last_signal_wins() -> None:
    from jri.core.opencode import parse_outcome

    lines = [
        _text_event("<!-- JRI:COMPLETED -->"),
        _text_event("actually blocked"),
        _text_event("<!-- JRI:BLOCKED -->"),
    ]
    assert parse_outcome(lines) == "blocked"


def test_parse_outcome_signal_embedded_in_text() -> None:
    from jri.core.opencode import parse_outcome

    lines = [
        _text_event("some preamble <!-- JRI:COMPLETED --> trailing"),
    ]
    assert parse_outcome(lines) == "completed"


def _text_event(text: str) -> str:
    return json.dumps(
        {"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": text}}
    )
