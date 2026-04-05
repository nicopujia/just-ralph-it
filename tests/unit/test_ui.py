from unittest.mock import patch

import pytest

from jri.core.ui import (
    iteration_footer,
    iteration_header,
    supports_color,
    trim_tool_output,
)


def test_iteration_header_contains_iteration_and_slug() -> None:
    result = iteration_header(3, "my-task")
    assert "3" in result
    assert "my-task" in result


def test_iteration_header_has_box_drawing_chars() -> None:
    result = iteration_header(1, "slug")
    assert "───" in result


def test_iteration_footer_completed() -> None:
    result = iteration_footer("completed")
    assert "completed" in result
    assert "✓" in result


def test_iteration_footer_failed() -> None:
    result = iteration_footer("failed")
    assert "failed" in result
    assert "✗" in result


def test_iteration_footer_needs_human() -> None:
    result = iteration_footer("needs human")
    assert "needs human" in result
    assert "⚠" in result


def test_iteration_footer_timeout() -> None:
    result = iteration_footer("timeout")
    assert "timeout" in result
    assert "⏱" in result


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
