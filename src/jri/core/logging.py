"""Local JSONL interview logging."""

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

type JsonValue = (
    bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
)

_SINGLE_SECRET_ENV_NAMES = ("OPENROUTER_API_KEY", "BRAVE_SEARCH_API_KEY")


class JsonlLogger:
    """Append interview events to a local JSONL log."""

    def __init__(self, path: Path) -> None:
        self.path: Path = path

    def write(self, event_type: str, data: Mapping[str, JsonValue]) -> None:
        """Append one event to the log."""
        event = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "type": event_type,
            "data": _redact(dict(data)),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as log_file:
            json.dump(event, log_file, ensure_ascii=False)
            log_file.write("\n")


def _redact(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, Mapping):
        return {str(key): _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _redact_string(value: str) -> str:
    redacted = value
    for name in _SINGLE_SECRET_ENV_NAMES:
        secret = os.environ.get(name)
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted
