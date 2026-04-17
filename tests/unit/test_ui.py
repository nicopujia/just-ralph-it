from unittest.mock import patch

import pytest

from jri.core.ui import (
    follow_status_bar,
    follow_status_bar_clear,
    supports_color,
    supports_interactive_footer,
    task_footer,
    task_header,
    trim_tool_output,
)


def test_task_header_contains_slug() -> None:
    result = task_header("my-task")
    assert "my-task" in result


def test_task_header_has_box_drawing_chars() -> None:
    result = task_header("slug")
    assert "───" in result


def test_task_footer_completed() -> None:
    result = task_footer("completed")
    assert "completed" in result
    assert "✓" in result


def test_task_footer_failed() -> None:
    result = task_footer("failed")
    assert "failed" in result
    assert "✗" in result


def test_task_footer_needs_human() -> None:
    result = task_footer("needs_human")
    assert "needs_human" in result
    assert "⚠" in result


def test_task_footer_timeout() -> None:
    result = task_footer("timeout")
    assert "timeout" in result
    assert "⏱" in result


def test_follow_status_bar_shows_task_and_controls() -> None:
    result = follow_status_bar("my-task")
    assert "task: my-task" in result
    assert "d detach" in result
    assert "s stop" in result
    assert "h halt" in result


def test_follow_status_bar_shows_halt_confirmation_prompt() -> None:
    result = follow_status_bar("my-task", confirming_halt=True)
    assert "task: my-task" in result
    assert "y then Enter" in result
    assert "n cancel" in result


def test_follow_status_bar_shows_stop_requested_feedback() -> None:
    result = follow_status_bar("my-task", stop_requested=True)
    assert "task: my-task" in result
    assert "d detach" in result
    assert "s stop (requested)" in result
    assert "h halt" in result


def test_follow_status_bar_shows_armed_halt_confirmation_prompt() -> None:
    result = follow_status_bar(
        "my-task",
        confirming_halt=True,
        halt_armed=True,
    )
    assert "Enter confirm" in result
    assert "n cancel" in result


def test_follow_status_bar_shows_active_subagent_spinner() -> None:
    result = follow_status_bar(
        "my-task",
        activity="research phase",
        spinner_frame="/",
    )
    assert "task: my-task" in result
    assert "/ research phase" in result


def test_follow_status_bar_clear_targets_bottom_row() -> None:
    assert follow_status_bar_clear(height=20) == "\0337\033[20;1H\033[2K\0338"


def test_trim_tool_output_returns_none_for_short_text() -> None:
    text = "line\n" * 5
    assert trim_tool_output(text) is None


def test_trim_tool_output_trims_long_text() -> None:
    text = "\n".join(f"line {i}" for i in range(50))
    result = trim_tool_output(text, max_lines=20, max_chars=10000)
    assert result is not None
    assert "… (" in result or "... (" in result
    assert "lines trimmed" in result


def test_trim_tool_output_trims_by_char_count() -> None:
    text = "a" * 3000
    result = trim_tool_output(text, max_lines=100, max_chars=2000)
    assert result is not None
    assert len(result) < len(text)


def test_trim_tool_output_handles_custom_thresholds() -> None:
    text = "\n".join(f"line {i}" for i in range(10))
    assert trim_tool_output(text, max_lines=5, max_chars=100000) is not None
    assert trim_tool_output(text, max_lines=20, max_chars=100000) is None


def test_supports_color_with_no_color_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    with patch("sys.stdout.isatty", return_value=True):
        assert supports_color() is False


def test_supports_color_with_force_color_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    with patch("sys.stdout.isatty", return_value=False):
        assert supports_color() is True


def test_supports_color_with_clicolor_force_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLICOLOR_FORCE", "1")
    with patch("sys.stdout.isatty", return_value=False):
        assert supports_color() is True


def test_supports_interactive_footer_requires_tty_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert supports_interactive_footer() is True
