"""Tests for local interview JSONL logging."""

import json
from pathlib import Path
from typing import cast

import pytest

from jri.core.logging import JsonlLogger


def test_jsonl_logger_writes_valid_event_objects(tmp_path: Path) -> None:
    """Logger writes one JSON object per event."""
    log_path = tmp_path / ".jri" / "logs" / "interview.jsonl"
    logger = JsonlLogger(log_path)

    logger.write("session_started", {"project_root": str(tmp_path)})

    line = log_path.read_text().strip()
    event = cast("dict[str, object]", json.loads(line))
    timestamp = event["ts"]
    assert isinstance(timestamp, str)
    assert event["type"] == "session_started"
    assert event["data"] == {"project_root": str(tmp_path)}
    assert timestamp.endswith("Z")


def test_jsonl_logger_redacts_openrouter_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Logger never writes configured API key values."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-secret")
    log_path = tmp_path / ".jri" / "logs" / "interview.jsonl"
    logger = JsonlLogger(log_path)

    logger.write(
        "error",
        {"message": "failed with openrouter-secret and brave-secret"},
    )

    log = log_path.read_text()
    assert "openrouter-secret" not in log
    assert "brave-secret" not in log
    assert "[redacted]" in log


def test_jsonl_logger_redacts_secret_keys_and_bearer_values(
    tmp_path: Path,
) -> None:
    """Raw trace payloads do not leak common secret fields."""
    log_path = tmp_path / ".jri" / "logs" / "interview.jsonl"
    logger = JsonlLogger(log_path)

    logger.write(
        "model_tool_call_started",
        {
            "api_key": "secret",
            "headers": {"Authorization": "Bearer abc123"},
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "message": "curl -H 'Authorization: Bearer xyz789'",
        },
    )

    log = log_path.read_text()
    assert "secret" not in log
    assert "abc123" not in log
    assert "xyz789" not in log
    assert "Bearer [redacted]" in log
    assert "input_tokens" in log
    assert "10" in log


def test_jsonl_logger_preserves_data_without_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Logger preserves nested JSON data when no secret is configured."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    log_path = tmp_path / ".jri" / "logs" / "interview.jsonl"
    logger = JsonlLogger(log_path)

    logger.write("tool_call_finished", {"items": ["safe", 1, None]})

    event = cast("dict[str, object]", json.loads(log_path.read_text()))
    assert event["data"] == {"items": ["safe", 1, None]}
