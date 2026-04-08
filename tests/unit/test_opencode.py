import json
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.error import URLError

import pytest

from jri.core.errors import JriError
from jri.core.opencode import OpenCodeServer, _parse_event_line


class _FakeSSEStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __enter__(self) -> "_FakeSSEStream":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._chunks)


class _OutcomeWritingSSEStream(_FakeSSEStream):
    def __init__(
        self,
        chunks: list[bytes],
        *,
        outcome_path: Path,
        outcome: str = "completed\n",
        write_index: int = 4,
    ) -> None:
        super().__init__(chunks)
        self._outcome_path = outcome_path
        self._outcome = outcome
        self._write_index = write_index

    def __iter__(self) -> Iterator[bytes]:
        for index, chunk in enumerate(self._chunks):
            if index == self._write_index:
                self._outcome_path.write_text(self._outcome, encoding="utf-8")
            yield chunk


class _HangingAfterChunksSSEStream(_FakeSSEStream):
    def __init__(self, chunks: list[bytes], *, delay: float) -> None:
        super().__init__(chunks)
        self._delay = delay

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks
        time.sleep(self._delay)


class _DelayedIdleOutcomeSSEStream(_FakeSSEStream):
    def __init__(
        self,
        chunks: list[bytes],
        *,
        outcome_path: Path,
        delay: float,
        split_index: int,
    ) -> None:
        super().__init__(chunks)
        self._outcome_path = outcome_path
        self._delay = delay
        self._split_index = split_index

    def __iter__(self) -> Iterator[bytes]:
        for index, chunk in enumerate(self._chunks):
            if index == self._split_index:
                time.sleep(self._delay)
                self._outcome_path.write_text("failed\n", encoding="utf-8")
            yield chunk


class _FakeProcess:
    def __init__(self, *, pid: int = 1234, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        assert timeout is None or timeout >= 0
        return 0 if self.returncode is None else self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _sse_event(payload: dict[str, object]) -> list[bytes]:
    return [f"data: {json.dumps(payload)}\n".encode(), b"\n"]


def test_start_uses_new_free_port_on_each_auto_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(binary="opencode")
    ports = iter([4101, 4102])
    commands: list[list[str]] = []
    processes: list[_FakeProcess] = []

    def fake_pick_free_local_port() -> int:
        return next(ports)

    def fake_popen(
        args: list[str], **kwargs: object
    ) -> _FakeProcess:
        assert kwargs["cwd"] == str(tmp_path)
        commands.append(args)
        process = _FakeProcess(pid=1000 + len(commands))
        processes.append(process)
        return process

    monkeypatch.setattr(
        "jri.core.opencode._pick_free_local_port", fake_pick_free_local_port
    )
    monkeypatch.setattr("jri.core.opencode.subprocess.Popen", fake_popen)
    monkeypatch.setattr(server, "is_healthy", lambda: True)

    server.start(cwd=tmp_path)
    assert server.port == 4101
    assert server._base_url == "http://127.0.0.1:4101"

    server.stop()

    server.start(cwd=tmp_path)

    assert server.port == 4102
    assert server._base_url == "http://127.0.0.1:4102"
    assert commands == [
        ["opencode", "serve", "--port", "4101"],
        ["opencode", "serve", "--port", "4102"],
    ]
    assert processes[0].terminated is True


def test_start_preserves_explicit_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(binary="opencode", port=5005)
    commands: list[list[str]] = []

    def fake_popen(args: list[str], **kwargs: object) -> _FakeProcess:
        assert kwargs["cwd"] == str(tmp_path)
        commands.append(args)
        return _FakeProcess()

    monkeypatch.setattr("jri.core.opencode.subprocess.Popen", fake_popen)
    monkeypatch.setattr(server, "is_healthy", lambda: True)

    server.start(cwd=tmp_path)

    assert server.port == 5005
    assert server._base_url == "http://127.0.0.1:5005"
    assert commands == [["opencode", "serve", "--port", "5005"]]


def test_run_ralph_task_rejects_session_for_different_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(binary="opencode")
    deleted: list[str] = []

    def fake_http_request(
        method: str,
        url: str,
        *,
        body: object | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, bytes]:
        assert method == "POST"
        assert "/session?directory=" in url
        return 201, json.dumps({"id": "ses_123", "directory": "/wrong/root"}).encode()

    def fake_delete_session(session_id: str) -> None:
        deleted.append(session_id)

    monkeypatch.setattr("jri.core.opencode._http_request", fake_http_request)
    monkeypatch.setattr(server, "_delete_session", fake_delete_session)

    with pytest.raises(
        JriError, match="opencode session was created for a different root"
    ):
        server.run_ralph_task(
            root=tmp_path,
            prompt="Solve the task",
            log_path=tmp_path / "ralph.log",
            outcome_path=tmp_path / "result.txt",
        )

    assert deleted == ["ses_123"]


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


def test_detect_result_tool_outcome_reads_server_tool_input() -> None:
    from jri.core.opencode import _detect_result_tool_outcome

    assert (
        _detect_result_tool_outcome(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "type": "tool",
                        "tool": "ralph-result",
                        "state": {
                            "input": {"outcome": "completed"},
                        },
                    }
                },
            }
        )
        == "completed"
    )


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


def test_run_ralph_task_ignores_stale_idle_until_run_becomes_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(binary="opencode")
    outcome_path = tmp_path / "result.txt"
    events = [
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "idle",
                },
            }
        ),
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "running",
                },
            }
        ),
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "idle",
                },
            }
        ),
    ]

    def fake_http_request(
        method: str,
        url: str,
        *,
        body: object | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, bytes]:
        if url.endswith("/prompt_async"):
            return 202, b"{}"
        return 201, b'{"id": "ses_123"}'

    def fake_urlopen(*args: object, **kwargs: object) -> object:
        return _OutcomeWritingSSEStream(events, outcome_path=outcome_path)

    monkeypatch.setattr("jri.core.opencode._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=tmp_path / "ralph.log",
        outcome_path=outcome_path,
    )

    assert result.outcome == "completed"
    assert result.warnings == []


def test_run_ralph_task_ignores_idle_after_non_running_pre_prompt_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(binary="opencode")
    outcome_path = tmp_path / "result.txt"
    events = [
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "loading",
                },
            }
        ),
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "idle",
                },
            }
        ),
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "running",
                },
            }
        ),
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "idle",
                },
            }
        ),
    ]

    def fake_http_request(
        method: str,
        url: str,
        *,
        body: object | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, bytes]:
        if url.endswith("/prompt_async"):
            return 202, b"{}"
        return 201, b'{"id": "ses_123"}'

    def fake_urlopen(*args: object, **kwargs: object) -> object:
        return _OutcomeWritingSSEStream(events, outcome_path=outcome_path)

    monkeypatch.setattr("jri.core.opencode._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=tmp_path / "ralph.log",
        outcome_path=outcome_path,
    )

    assert result.outcome == "completed"
    assert result.warnings == []


def test_run_ralph_task_treats_busy_as_active_for_idle_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(binary="opencode")
    outcome_path = tmp_path / "result.txt"
    events = [
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "idle",
                },
            }
        ),
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "busy",
                },
            }
        ),
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "idle",
                },
            }
        ),
    ]

    def fake_http_request(
        method: str,
        url: str,
        *,
        body: object | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, bytes]:
        if url.endswith("/prompt_async"):
            return 202, b"{}"
        return 201, b'{"id": "ses_123"}'

    def fake_urlopen(*args: object, **kwargs: object) -> object:
        return _OutcomeWritingSSEStream(events, outcome_path=outcome_path)

    monkeypatch.setattr("jri.core.opencode._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=tmp_path / "ralph.log",
        outcome_path=outcome_path,
        timeout=1,
    )

    assert result.outcome == "completed"
    assert result.returncode == 0
    assert result.warnings == []


def test_run_ralph_task_falls_back_to_result_tool_outcome_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(binary="opencode")
    outcome_path = tmp_path / "result.txt"
    events = [
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "running",
                },
            }
        ),
        *_sse_event(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "type": "tool",
                        "tool": "ralph-result",
                        "state": {
                            "status": "completed",
                            "input": {"outcome": "completed"},
                            "output": "JRI_OUTCOME_PATH not set",
                        },
                    }
                },
            }
        ),
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "idle",
                },
            }
        ),
    ]

    def fake_http_request(
        method: str,
        url: str,
        *,
        body: object | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, bytes]:
        if url.endswith("/prompt_async"):
            return 202, b"{}"
        return 201, b'{"id": "ses_123"}'

    def fake_urlopen(*args: object, **kwargs: object) -> object:
        return _FakeSSEStream(events)

    monkeypatch.setattr("jri.core.opencode._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=tmp_path / "ralph.log",
        outcome_path=outcome_path,
    )

    assert not outcome_path.exists()
    assert result.outcome == "completed"
    assert result.warnings == []


def test_run_ralph_task_waits_for_idle_after_terminal_result_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(binary="opencode")
    outcome_path = tmp_path / "result.txt"
    events = [
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "running",
                },
            }
        ),
        *_sse_event(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "type": "tool",
                        "tool": "ralph-result",
                        "state": {
                            "status": "completed",
                            "input": {"outcome": "completed"},
                        },
                    }
                },
            }
        ),
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "idle",
                },
            }
        ),
    ]

    def fake_http_request(
        method: str,
        url: str,
        *,
        body: object | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, bytes]:
        if url.endswith("/prompt_async"):
            return 202, b"{}"
        return 201, b'{"id": "ses_123"}'

    def fake_urlopen(*args: object, **kwargs: object) -> object:
        return _DelayedIdleOutcomeSSEStream(
            events,
            outcome_path=outcome_path,
            delay=0.05,
            split_index=4,
        )

    monkeypatch.setattr("jri.core.opencode._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=tmp_path / "ralph.log",
        outcome_path=outcome_path,
        timeout=1,
    )

    assert result.outcome == "failed"
    assert result.returncode == 0
    assert result.warnings == []


def test_run_ralph_task_times_out_after_terminal_result_tool_without_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(binary="opencode")
    outcome_path = tmp_path / "result.txt"
    events = [
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "running",
                },
            }
        ),
        *_sse_event(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "type": "tool",
                        "tool": "ralph-result",
                        "state": {
                            "status": "completed",
                            "input": {"outcome": "completed"},
                        },
                    }
                },
            }
        ),
    ]

    def fake_http_request(
        method: str,
        url: str,
        *,
        body: object | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, bytes]:
        if url.endswith("/prompt_async"):
            return 202, b"{}"
        return 201, b'{"id": "ses_123"}'

    def fake_urlopen(*args: object, **kwargs: object) -> object:
        return _HangingAfterChunksSSEStream(events, delay=2.0)

    monkeypatch.setattr("jri.core.opencode._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=tmp_path / "ralph.log",
        outcome_path=outcome_path,
        timeout=1,
    )

    assert result.outcome == "timeout"
    assert result.returncode == -1
    assert result.warnings == ["opencode prompt killed after 1s timeout"]


def test_run_ralph_task_prefers_outcome_file_over_result_tool_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(binary="opencode")
    outcome_path = tmp_path / "result.txt"
    events = [
        *_sse_event(
            {
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "running",
                },
            }
        ),
        *_sse_event(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "type": "tool",
                        "tool": "ralph-result",
                        "state": {
                            "status": "completed",
                            "input": {"outcome": "completed"},
                        },
                    }
                },
            }
        ),
    ]

    def fake_http_request(
        method: str,
        url: str,
        *,
        body: object | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, bytes]:
        if url.endswith("/prompt_async"):
            return 202, b"{}"
        return 201, b'{"id": "ses_123"}'

    def fake_urlopen(*args: object, **kwargs: object) -> object:
        return _OutcomeWritingSSEStream(
            events,
            outcome_path=outcome_path,
            outcome="failed\n",
            write_index=2,
        )

    monkeypatch.setattr("jri.core.opencode._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=tmp_path / "ralph.log",
        outcome_path=outcome_path,
    )

    assert result.outcome == "failed"
    assert result.warnings == []


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
