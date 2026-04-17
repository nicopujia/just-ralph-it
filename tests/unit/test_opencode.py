import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from urllib.error import URLError

import pytest

from jri.core.errors import JriError
from jri.core.opencode import (
    OpenCodeServer,
    SavedLogRenderer,
    _parse_event_line,
    launch_chat,
    render_saved_log,
)


def _result_payload(result: str = "completed", **extra: object) -> str:
    payload: dict[str, object] = {"result": result}
    payload.update(extra)
    return json.dumps(payload) + "\n"


class _FakeSSEStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __enter__(self) -> "_FakeSSEStream":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._chunks)


class _ResultWritingSSEStream(_FakeSSEStream):
    def __init__(
        self,
        chunks: list[bytes],
        *,
        result_path: Path,
        result_text: str = _result_payload(),
        write_index: int = 4,
    ) -> None:
        super().__init__(chunks)
        self._result_path = result_path
        self._result_text = result_text
        self._write_index = write_index

    def __iter__(self) -> Iterator[bytes]:
        for index, chunk in enumerate(self._chunks):
            if index == self._write_index:
                self._result_path.write_text(self._result_text, encoding="utf-8")
            yield chunk


class _HangingAfterChunksSSEStream(_FakeSSEStream):
    def __init__(self, chunks: list[bytes], *, delay: float) -> None:
        super().__init__(chunks)
        self._delay = delay

    def __iter__(self) -> Iterator[bytes]:
        yield from self._chunks
        time.sleep(self._delay)


class _DelayedIdleResultSSEStream(_FakeSSEStream):
    def __init__(
        self,
        chunks: list[bytes],
        *,
        result_path: Path,
        delay: float,
        split_index: int,
    ) -> None:
        super().__init__(chunks)
        self._result_path = result_path
        self._delay = delay
        self._split_index = split_index

    def __iter__(self) -> Iterator[bytes]:
        for index, chunk in enumerate(self._chunks):
            if index == self._split_index:
                time.sleep(self._delay)
                self._result_path.write_text(
                    _result_payload("incompleted"), encoding="utf-8"
                )
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


def test_render_saved_log_replays_streamed_text_and_tool_labels() -> None:
    log = "\n".join(
        [
            json.dumps(
                {
                    "type": "message.part.updated",
                    "properties": {
                        "part": {
                            "type": "tool",
                            "id": "tool-1",
                            "tool": "read",
                            "state": {
                                "status": "running",
                                "input": {"filePath": ".jri/tasks/doing/task-a.md"},
                            },
                        }
                    },
                }
            ),
            json.dumps(
                {
                    "type": "message.part.delta",
                    "properties": {"field": "text", "delta": "Working"},
                }
            ),
            json.dumps(
                {
                    "type": "message.part.updated",
                    "properties": {"part": {"type": "step-finish"}},
                }
            ),
        ]
    )

    rendered = render_saved_log(log)

    assert "⚙ read .jri/tasks/doing/task-a.md" in rendered
    assert "Working" in rendered


def test_saved_log_renderer_handles_partial_chunks() -> None:
    renderer = SavedLogRenderer()
    tool_line = json.dumps(
        {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "type": "tool",
                    "id": "tool-1",
                    "tool": "read",
                    "state": {
                        "status": "running",
                        "input": {"filePath": ".jri/tasks/doing/task-a.md"},
                    },
                }
            },
        }
    )
    text_line = json.dumps(
        {
            "type": "message.part.delta",
            "properties": {"field": "text", "delta": "Spawned research subagent"},
        }
    )

    assert renderer.render_chunk(tool_line[:25]) == ""

    rendered = renderer.render_chunk(tool_line[25:] + "\n" + text_line[:20])
    assert "⚙ read .jri/tasks/doing/task-a.md" in rendered
    assert '"type"' not in rendered

    rendered += renderer.render_chunk(text_line[20:], final=True)
    assert "Spawned research subagent" in rendered


def test_saved_log_renderer_keeps_task_stdout_and_tracks_active_task() -> None:
    renderer = SavedLogRenderer()
    running_line = json.dumps(
        {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "type": "tool",
                    "id": "tool-1",
                    "tool": "task",
                    "state": {
                        "status": "running",
                        "input": {"description": "research phase"},
                    },
                }
            },
        }
    )
    task_text_line = json.dumps(
        {
            "type": "message.part.delta",
            "properties": {"field": "text", "delta": "Spawned research subagent"},
        }
    )
    completed_line = json.dumps(
        {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "type": "tool",
                    "id": "tool-1",
                    "tool": "task",
                    "state": {"status": "completed"},
                }
            },
        }
    )
    main_text_line = json.dumps(
        {
            "type": "message.part.delta",
            "properties": {"field": "text", "delta": "Back in the main agent"},
        }
    )

    rendered = renderer.render_chunk(f"{running_line}\n{task_text_line}\n")

    assert "⚙ task research phase" in rendered
    assert "Spawned research subagent" in rendered
    assert renderer.active_task_detail == "research phase"

    rendered = renderer.render_chunk(
        f"{completed_line}\n{main_text_line}\n",
        final=True,
    )

    assert "Back in the main agent" in rendered
    assert renderer.active_task_detail is None


def test_render_saved_log_keeps_task_tool_labels_dim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jri.core.ui.supports_color", lambda: True)
    log = json.dumps(
        {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "type": "tool",
                    "id": "tool-1",
                    "tool": "task",
                    "state": {
                        "status": "running",
                        "input": {"description": "research phase"},
                    },
                }
            },
        }
    )

    rendered = render_saved_log(log)

    assert "\033[2m⚙ task research phase\033[0m" in rendered


def test_render_saved_log_keeps_non_task_tool_labels_dim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jri.core.ui.supports_color", lambda: True)
    log = json.dumps(
        {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "type": "tool",
                    "id": "tool-1",
                    "tool": "read",
                    "state": {
                        "status": "running",
                        "input": {"filePath": ".jri/tasks/doing/task-a.md"},
                    },
                }
            },
        }
    )

    rendered = render_saved_log(log)

    assert "\033[2m⚙ read .jri/tasks/doing/task-a.md\033[0m" in rendered
    assert "\033[36m⚙ read .jri/tasks/doing/task-a.md" not in rendered


def test_start_uses_new_free_port_on_each_auto_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(binary="opencode")
    ports = iter([4101, 4102])
    commands: list[list[str]] = []
    processes: list[_FakeProcess] = []

    def fake_pick_free_local_port() -> int:
        return next(ports)

    def fake_popen(args: list[str], **kwargs: object) -> _FakeProcess:
        assert kwargs["cwd"] == str(tmp_path)
        commands.append(args)
        process = _FakeProcess(pid=1000 + len(commands))
        processes.append(process)
        return process

    monkeypatch.setattr(
        "jri.core.opencode.client._pick_free_local_port", fake_pick_free_local_port
    )
    monkeypatch.setattr("jri.core.opencode.client.subprocess.Popen", fake_popen)
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

    monkeypatch.setattr("jri.core.opencode.client.subprocess.Popen", fake_popen)
    monkeypatch.setattr(server, "is_healthy", lambda: True)

    server.start(cwd=tmp_path)

    assert server.port == 5005
    assert server._base_url == "http://127.0.0.1:5005"
    assert commands == [["opencode", "serve", "--port", "5005"]]


def test_start_merges_custom_env_for_server_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OpenCodeServer(binary="opencode", port=5005)
    popen_env: dict[str, str] = {}

    def fake_popen(args: list[str], **kwargs: object) -> _FakeProcess:
        assert args == ["opencode", "serve", "--port", "5005"]
        assert kwargs["cwd"] == str(tmp_path)
        env = kwargs["env"]
        assert isinstance(env, dict)
        for key, value in env.items():
            popen_env[str(key)] = str(value)
        return _FakeProcess()

    monkeypatch.setattr("jri.core.opencode.client.subprocess.Popen", fake_popen)
    monkeypatch.setattr(server, "is_healthy", lambda: True)
    monkeypatch.setenv("BASE_ENV", "from-os")

    server.start(cwd=tmp_path, env={"JRI_RESULT_PATH": "result.json"})

    assert popen_env["BASE_ENV"] == "from-os"
    assert popen_env["JRI_RESULT_PATH"] == "result.json"


def test_launch_chat_merges_custom_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> _FakeProcess:
        captured["args"] = args
        captured["cwd"] = kwargs["cwd"]
        captured["env"] = kwargs["env"]
        return _FakeProcess(returncode=0)

    monkeypatch.setattr("jri.core.opencode.client.subprocess.run", fake_run)
    monkeypatch.setenv("BASE_ENV", "from-os")

    result = launch_chat(
        root=tmp_path,
        session_id="ses_123",
        extra_args=["--model", "test-model"],
        env={"OPENCODE_CONFIG": "/tmp/config.json"},
    )

    assert result == 0
    assert captured["args"] == [
        "opencode",
        str(tmp_path),
        "--agent",
        "interrogator",
        "--session",
        "ses_123",
        "--model",
        "test-model",
    ]
    assert captured["cwd"] == tmp_path
    env = captured["env"]
    assert isinstance(env, dict)
    merged_env = {str(key): str(value) for key, value in env.items()}
    assert merged_env["BASE_ENV"] == "from-os"
    assert merged_env["OPENCODE_CONFIG"] == "/tmp/config.json"


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

    monkeypatch.setattr("jri.core.opencode.client._http_request", fake_http_request)
    monkeypatch.setattr(server, "_delete_session", fake_delete_session)

    with pytest.raises(
        JriError, match="opencode session was created for a different root"
    ):
        server.run_ralph_task(
            root=tmp_path,
            prompt="Solve the task",
            log_path=tmp_path / "ralph.log",
            result_path=tmp_path / "result.txt",
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


def test_missing_result_payload_treats_run_as_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jri.core.opencode import _missing_result_payload

    result, warnings = _missing_result_payload(context="Ralph run")
    assert result == "failed"
    msg = "missing result payload for Ralph run; treating run as failed"
    assert warnings == [msg]
    assert msg in capsys.readouterr().err


def test_parse_result_payload_rejects_malformed_needs_human_human_task(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jri.core.opencode import _parse_result_payload

    payload, warnings = _parse_result_payload(
        json.dumps(
            {
                "result": "needs_human",
                "blocker": "Waiting on human input",
                "human_task": {
                    "title": 123,
                    "body": "Please help",
                    "acceptance_criteria": ["done"],
                },
            }
        )
    )

    assert payload is None
    assert warnings == [
        "invalid result payload; treating run as failed: "
        "`human_task.title` must be a non-empty string"
    ]
    assert "`human_task.title` must be a non-empty string" in capsys.readouterr().err


def test_parse_result_payload_rejects_failed_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from jri.core.opencode import _parse_result_payload

    payload, warnings = _parse_result_payload(_result_payload("failed"))

    assert payload is None
    assert warnings == [
        "invalid result payload; treating run as failed: missing or unknown `result`"
    ]
    assert "missing or unknown `result`" in capsys.readouterr().err


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

    monkeypatch.setattr("jri.core.opencode.client._http_request", fake_http_request)

    with pytest.raises(
        JriError, match=r"failed to create opencode session \(HTTP 500\): boom"
    ):
        server.run_ralph_task(
            root=tmp_path,
            prompt="Solve the task",
            log_path=tmp_path / "ralph.log",
            result_path=tmp_path / "result.txt",
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

    monkeypatch.setattr("jri.core.opencode.client._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.client.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(server, "_delete_session", fake_delete_session)

    with pytest.raises(JriError, match="failed to start ralph prompt"):
        server.run_ralph_task(
            root=tmp_path,
            prompt="Solve the task",
            log_path=tmp_path / "ralph.log",
            result_path=tmp_path / "result.txt",
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
        return _ResultWritingSSEStream(events, result_path=outcome_path)

    monkeypatch.setattr("jri.core.opencode.client._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.client.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=tmp_path / "ralph.log",
        result_path=outcome_path,
    )

    assert result.result == "completed"
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
        return _ResultWritingSSEStream(events, result_path=outcome_path)

    monkeypatch.setattr("jri.core.opencode.client._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.client.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=tmp_path / "ralph.log",
        result_path=outcome_path,
    )

    assert result.result == "completed"
    assert result.warnings == []


def test_run_ralph_task_prints_task_stdout_and_keeps_raw_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = OpenCodeServer(binary="opencode")
    log_path = tmp_path / "ralph.log"
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
                        "id": "tool-1",
                        "tool": "task",
                        "state": {
                            "status": "running",
                            "input": {"description": "research phase"},
                        },
                    }
                },
            }
        ),
        *_sse_event(
            {
                "type": "message.part.delta",
                "properties": {
                    "field": "text",
                    "delta": "Spawned research subagent",
                },
            }
        ),
        *_sse_event(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "type": "tool",
                        "id": "tool-1",
                        "tool": "task",
                        "state": {"status": "completed"},
                    }
                },
            }
        ),
        *_sse_event(
            {
                "type": "message.part.delta",
                "properties": {
                    "field": "text",
                    "delta": "Back in the main agent",
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
        return _ResultWritingSSEStream(events, result_path=outcome_path, write_index=10)

    monkeypatch.setattr("jri.core.opencode.client._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.client.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=log_path,
        result_path=outcome_path,
    )

    assert result.result == "completed"
    output = capsys.readouterr().out
    assert output == (
        "⚙ task research phase\nSpawned research subagentBack in the main agent"
    )
    log_text = log_path.read_text(encoding="utf-8")
    assert "Spawned research subagent" in log_text
    assert "research phase" in log_text


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
        return _ResultWritingSSEStream(events, result_path=outcome_path)

    monkeypatch.setattr("jri.core.opencode.client._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.client.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=tmp_path / "ralph.log",
        result_path=outcome_path,
        timeout=1,
    )

    assert result.result == "completed"
    assert result.returncode == 0
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
                            "input": {"result": "completed"},
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
        return _DelayedIdleResultSSEStream(
            events,
            result_path=outcome_path,
            delay=0.05,
            split_index=4,
        )

    monkeypatch.setattr("jri.core.opencode.client._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.client.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=tmp_path / "ralph.log",
        result_path=outcome_path,
        timeout=1,
    )

    assert result.result == "incompleted"
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
                            "input": {"result": "completed"},
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

    monkeypatch.setattr("jri.core.opencode.client._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.client.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=tmp_path / "ralph.log",
        result_path=outcome_path,
        timeout=1,
    )

    assert result.result == "timeout"
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
                            "input": {"result": "completed"},
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
        return _ResultWritingSSEStream(
            events,
            result_path=outcome_path,
            result_text=_result_payload("incompleted"),
            write_index=2,
        )

    monkeypatch.setattr("jri.core.opencode.client._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.client.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=tmp_path / "ralph.log",
        result_path=outcome_path,
    )

    assert result.result == "incompleted"
    assert result.warnings == []


def test_run_ralph_task_retries_missing_result_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jri.core.opencode.client import _MISSING_RESULT_FOLLOW_UP_PROMPT

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
    prompt_bodies: list[object] = []

    def fake_http_request(
        method: str,
        url: str,
        *,
        body: object | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, bytes]:
        del method, timeout
        if url.endswith("/prompt_async"):
            assert isinstance(body, dict)
            prompt_bodies.append(body)
            if len(prompt_bodies) == 2:
                outcome_path.write_text(_result_payload("completed"), encoding="utf-8")
            return 202, b"{}"
        return 201, b'{"id": "ses_123"}'

    def fake_urlopen(*args: object, **kwargs: object) -> object:
        return _FakeSSEStream(events)

    monkeypatch.setattr("jri.core.opencode.client._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.client.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=tmp_path / "ralph.log",
        result_path=outcome_path,
    )

    assert result.result == "completed"
    assert len(prompt_bodies) == 2
    assert isinstance(prompt_bodies[0], dict)
    assert isinstance(prompt_bodies[1], dict)
    first_prompt = cast(dict[str, object], dict(prompt_bodies[0]))
    second_prompt = cast(dict[str, object], dict(prompt_bodies[1]))
    assert first_prompt["parts"] == [{"type": "text", "text": "Solve the task"}]
    assert second_prompt["parts"] == [
        {"type": "text", "text": _MISSING_RESULT_FOLLOW_UP_PROMPT}
    ]


def test_run_ralph_task_fails_after_missing_result_follow_up(
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
                "type": "session.status",
                "properties": {
                    "sessionID": "ses_123",
                    "status": "idle",
                },
            }
        ),
    ]
    prompt_calls = 0

    def fake_http_request(
        method: str,
        url: str,
        *,
        body: object | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, bytes]:
        del method, body, timeout
        nonlocal prompt_calls
        if url.endswith("/prompt_async"):
            prompt_calls += 1
            return 202, b"{}"
        return 201, b'{"id": "ses_123"}'

    def fake_urlopen(*args: object, **kwargs: object) -> object:
        return _FakeSSEStream(events)

    monkeypatch.setattr("jri.core.opencode.client._http_request", fake_http_request)
    monkeypatch.setattr("jri.core.opencode.client.urllib.request.urlopen", fake_urlopen)

    result = server.run_ralph_task(
        root=tmp_path,
        prompt="Solve the task",
        log_path=tmp_path / "ralph.log",
        result_path=outcome_path,
    )

    assert result.result == "failed"
    assert prompt_calls == 2
    assert result.warnings == [
        "missing result payload for Ralph run; treating run as failed"
    ]


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
