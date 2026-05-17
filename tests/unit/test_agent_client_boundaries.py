import io
import json
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

import jri.core.agents.client as client_module
from jri.core.agents.client import (
    PiRuntime,
    _compiler_event_text,
    _message_content_text,
    _readline_with_timeout,
    launch_chat,
    parse_event_line,
    parse_result_payload,
    render_saved_event,
)
from jri.core.errors import JriError


@pytest.fixture(autouse=True)
def forbid_live_pi_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_popen(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("unit tests must not spawn pi")

    monkeypatch.setattr("jri.core.agents.client.subprocess.Popen", fail_popen)


class FakeProcess:
    def __init__(self, *, poll_result: int | None = None) -> None:
        self.pid = 4321
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.poll_result = poll_result
        self.terminated = False
        self.killed = False
        self.wait_timeouts: list[int | float | None] = []

    def poll(self) -> int | None:
        return self.poll_result

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: int | float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        return 0


class TimeoutThenExitProcess(FakeProcess):
    def wait(self, timeout: int | float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if len(self.wait_timeouts) == 1:
            raise subprocess.TimeoutExpired(cmd="pi", timeout=0 if timeout is None else timeout)
        return 0


class AlwaysTimeoutProcess(FakeProcess):
    def wait(self, timeout: int | float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        raise subprocess.TimeoutExpired(cmd="pi", timeout=0 if timeout is None else timeout)


def write_session_file(repo: Path, session_id: str, *, cwd: Path | None = None) -> Path:
    session_dir = repo / ".jri" / "logs" / "chat" / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{session_id}.jsonl"
    session_file.write_text(
        json.dumps({"type": "session", "id": session_id, "cwd": str(cwd or repo)})
        + "\n"
        + json.dumps({"type": "message_update", "delta": "hello"})
        + "\n",
        encoding="utf-8",
    )
    return session_file


def test_render_saved_event_ignores_malformed_events_and_deduplicates_tools() -> None:
    seen: set[str] = set()

    ignored_events: list[dict[str, object]] = [
        {"type": "message.part.delta"},
        {"type": "message.part.delta", "properties": {"field": "thinking"}},
        {"type": "message.part.updated"},
        {"type": "message.part.updated", "properties": {"part": "tool"}},
        {"type": "message.part.updated", "properties": {"part": {"type": "text"}}},
        {"type": "message.part.updated", "properties": {"part": {"type": "tool"}}},
        {"type": "message.part.updated", "properties": {"part": {"type": "tool", "state": {"status": "done"}}}},
        {"type": "message_start"},
        {"type": "unknown"},
    ]

    for event in ignored_events:
        assert render_saved_event(event, seen_tool_calls=seen) == ("", False)

    text, newline_before = render_saved_event(
        {"type": "tool_execution_start", "toolCallId": "call-1", "toolName": "bash", "input": {"command": "x" * 90}},
        seen_tool_calls=seen,
    )

    assert newline_before is True
    assert "bash " in text
    assert "..." in text
    assert render_saved_event({"type": "tool_execution_update", "toolCallId": "call-1"}, seen_tool_calls=seen) == (
        "",
        False,
    )

    text, newline_before = render_saved_event(
        {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "type": "tool",
                    "id": "call-2",
                    "tool": "read",
                    "state": {"status": "running", "input": {"path": "/outside.py"}},
                }
            },
        },
        seen_tool_calls=seen,
        cwd_hint="/repo/",
    )
    assert newline_before is True
    assert "read /outside.py" in text
    assert (
        render_saved_event(
            {"type": "tool_execution_start", "toolName": "todowrite", "input": {"todos": []}}, seen_tool_calls=seen
        )[1]
        is True
    )

    text, newline_before = render_saved_event(
        {
            "type": "message.part.updated",
            "properties": {"part": {"type": "tool", "tool": "bash", "state": {"status": "running"}}},
        },
        seen_tool_calls=seen,
    )
    assert newline_before is True
    assert "bash" in text

    text, newline_before = render_saved_event(
        {
            "type": "message.part.updated",
            "properties": {
                "part": {"type": "tool", "id": "call-3", "tool": "unknown-tool", "state": {"status": "running"}}
            },
        },
        seen_tool_calls=seen,
    )
    assert newline_before is True
    assert "unknown-tool" in text


def test_saved_log_renderer_preserves_plain_text_and_ignores_non_rendered_json() -> None:
    renderer = client_module.SavedLogRenderer()

    assert renderer.render_chunk("partial") == ""
    assert renderer.render_chunk(" line\n") == "partial line\n"
    assert renderer.render_chunk("[]\n") == ""
    assert renderer.render_chunk(json.dumps({"type": "message_start"}) + "\n") == ""

    rendered: list[str] = []
    renderer._append_line(rendered, "")
    assert rendered == [""]


def test_saved_log_renderer_task_tracking_guards_and_legacy_task_events() -> None:
    renderer = client_module.SavedLogRenderer()

    malformed_task_updates: list[dict[str, object]] = [
        {"type": "message.part.updated"},
        {"type": "message.part.updated", "properties": {"part": "task"}},
        {"type": "message.part.updated", "properties": {"part": {"type": "text"}}},
        {"type": "message.part.updated", "properties": {"part": {"type": "tool", "tool": "task"}}},
        {
            "type": "message.part.updated",
            "properties": {"part": {"type": "tool", "tool": "task", "state": {"status": "done"}}},
        },
    ]
    for event in malformed_task_updates:
        assert renderer.render_event(event) == ("", False)
        assert renderer.active_task_detail is None

    text, newline_before = renderer.render_event({
        "type": "tool_execution_start",
        "toolName": "task",
        "id": "task-1",
        "input": {"description": "inspect"},
    })

    assert newline_before is True
    assert "task inspect" in text
    assert renderer.active_task_detail == "inspect"


@pytest.mark.parametrize(
    ("human_task", "warning_text"),
    [
        ("not-an-object", "requires `blocker` and `human_task`"),
        ({"title": "", "body": "Body", "acceptance_criteria": ["ok"]}, "title"),
        ({"title": "Title", "body": "", "acceptance_criteria": ["ok"]}, "body"),
        ({"title": "Title", "body": "Body", "acceptance_criteria": []}, "acceptance_criteria"),
        ({"title": "Title", "body": "Body", "acceptance_criteria": [""]}, "acceptance_criteria"),
        ({"title": "Title", "body": "Body", "acceptance_criteria": ["ok"], "priority": True}, "priority"),
        ({"title": "Title", "body": "Body", "acceptance_criteria": ["ok"], "priority": 5}, "priority"),
    ],
)
def test_parse_result_payload_reports_invalid_human_task_variants(
    human_task: object, warning_text: str, capsys: pytest.CaptureFixture[str]
) -> None:
    payload, warnings = parse_result_payload(
        json.dumps({"result": "needs_human", "blocker": "blocked", "human_task": human_task})
    )

    assert payload is None
    assert warnings and warning_text in warnings[0]
    assert warning_text in capsys.readouterr().err


def test_parse_event_line_returns_payload_without_text_for_non_rendered_event() -> None:
    payload, text, is_tool_output = parse_event_line('{"type":"message_start"}')

    assert payload == {"type": "message_start"}
    assert text is None
    assert is_tool_output is False


def test_parse_event_line_ignores_empty_tool_output() -> None:
    payload, text, is_tool_output = parse_event_line('{"type":"tool_execution_update","output":""}')

    assert payload == {"type": "tool_execution_update", "output": ""}
    assert text is None
    assert is_tool_output is False


def test_human_task_payload_validation_rejects_non_object() -> None:
    assert (
        client_module._validate_human_task_payload({"human_task": "not-an-object"}) == "`human_task` must be an object"
    )


def test_launch_chat_reports_missing_binary_and_preserves_package_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    commands: list[list[str]] = []

    def fake_run(
        command: list[str], *, cwd: Path, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert cwd == tmp_path
        assert env["JRI_PYTHON"]
        assert env["JRI_CHAT_RUNTIME"] == "1"
        assert check is False
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(client_module.subprocess, "run", fake_run)

    assert (
        launch_chat(
            root=tmp_path,
            session_id=None,
            extra_args=["--model", "fake"],
            binary="pi-fake",
            env={"JRI_PI_PACKAGE": str(package_root)},
        )
        == 0
    )
    assert commands[0][0] == "pi-fake"
    assert "--no-extensions" in commands[0]
    assert "--model" in commands[0]

    def missing_binary(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise FileNotFoundError

    monkeypatch.setattr(client_module.subprocess, "run", missing_binary)
    with pytest.raises(JriError, match="could not find `pi-missing`"):
        launch_chat(root=tmp_path, session_id=None, extra_args=[], binary="pi-missing")


def test_pi_runtime_stop_returns_when_no_process(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = PiRuntime(binary="pi")

    def fail_getpgid(pid: int) -> int:
        del pid
        raise AssertionError("no process should not query a process group")

    monkeypatch.setattr("jri.core.agents.client.os.getpgid", fail_getpgid)

    runtime.stop()

    assert runtime._process is None


def test_pi_runtime_stop_drops_already_exited_process(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = PiRuntime(binary="pi")
    process = FakeProcess(poll_result=7)
    runtime._process = cast(Any, process)

    def fail_getpgid(pid: int) -> int:
        del pid
        raise AssertionError("exited process should not query a process group")

    monkeypatch.setattr("jri.core.agents.client.os.getpgid", fail_getpgid)

    runtime.stop()

    assert runtime._process is None
    assert process.wait_timeouts == []
    assert process.terminated is False
    assert process.killed is False


def test_pi_runtime_stop_terminates_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = PiRuntime(binary="pi")
    process = FakeProcess()
    runtime._process = cast(Any, process)
    signals: list[tuple[int, signal.Signals]] = []

    def fake_getpgid(pid: int) -> int:
        assert pid == process.pid
        return 9876

    monkeypatch.setattr("jri.core.agents.client.os.getpgid", fake_getpgid)

    def fake_killpg(pgid: int, sig: int | signal.Signals) -> None:
        signals.append((pgid, cast(signal.Signals, sig)))

    monkeypatch.setattr("jri.core.agents.client.os.killpg", fake_killpg)

    runtime.stop()

    assert runtime._process is None
    assert signals == [(9876, signal.SIGTERM)]
    assert process.wait_timeouts == [5]
    assert process.terminated is False
    assert process.killed is False


def test_pi_runtime_stop_kills_process_after_terminate_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = PiRuntime(binary="pi")
    process = TimeoutThenExitProcess()
    runtime._process = cast(Any, process)

    def missing_process_group(pid: int) -> int:
        del pid
        raise ProcessLookupError

    monkeypatch.setattr("jri.core.agents.client.os.getpgid", missing_process_group)

    runtime.stop()

    assert runtime._process is None
    assert process.terminated is True
    assert process.killed is True
    assert process.wait_timeouts == [5, 5]


def test_pi_runtime_stop_swallows_process_cleanup_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = PiRuntime(binary="pi")
    process = AlwaysTimeoutProcess()
    runtime._process = cast(Any, process)

    def fake_getpgid(pid: int) -> int:
        del pid
        return 9876

    monkeypatch.setattr(client_module.os, "getpgid", fake_getpgid)

    def fail_killpg(pgid: int, sig: int | signal.Signals) -> None:
        del pgid, sig
        raise RuntimeError("cannot signal")

    monkeypatch.setattr(client_module.os, "killpg", fail_killpg)

    runtime.stop()

    assert runtime._process is None


def test_pi_runtime_stop_swallows_repeated_process_group_wait_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = PiRuntime(binary="pi")
    process = AlwaysTimeoutProcess()
    runtime._process = cast(Any, process)
    signals: list[signal.Signals] = []

    def fake_getpgid(pid: int) -> int:
        del pid
        return 9876

    monkeypatch.setattr(client_module.os, "getpgid", fake_getpgid)

    def fake_killpg(pgid: int, sig: int | signal.Signals) -> None:
        assert pgid == 9876
        signals.append(cast(signal.Signals, sig))

    monkeypatch.setattr(client_module.os, "killpg", fake_killpg)

    runtime.stop()

    assert runtime._process is None
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert process.wait_timeouts == [5, 5]


def test_pi_runtime_start_reuses_healthy_process_and_reports_start_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PiRuntime(binary="pi", model="fake-model")
    process = FakeProcess()
    runtime._process = cast(Any, process)

    def fail_popen(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("healthy process should be reused")

    monkeypatch.setattr(client_module.subprocess, "Popen", fail_popen)
    runtime.start(cwd=tmp_path)
    assert runtime._process is process

    runtime._process = None

    def missing_binary(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise FileNotFoundError

    monkeypatch.setattr(client_module.subprocess, "Popen", missing_binary)
    with pytest.raises(JriError, match="could not find `pi`"):
        runtime.start(cwd=tmp_path, extra_args=["--flag"])

    class NoStreamsProcess(FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self.stdin = cast(Any, None)

    stopped: list[bool] = []

    def no_streams_popen(*args: object, **kwargs: object) -> NoStreamsProcess:
        command = cast(list[str], args[0])
        assert command[:4] == ["pi", "--mode", "rpc", "--model"]
        assert "--session-dir" in command
        return NoStreamsProcess()

    monkeypatch.setattr(client_module.subprocess, "Popen", no_streams_popen)
    monkeypatch.setattr(runtime, "stop", lambda: stopped.append(True))
    with pytest.raises(JriError, match="failed to start pi rpc process"):
        runtime.start(cwd=tmp_path)
    assert stopped == [True]


def test_pi_runtime_start_records_session_state_from_rpc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = PiRuntime(binary="pi")
    package_root = tmp_path / "package"
    (package_root / "ralph" / "skills" / "alpha").mkdir(parents=True)
    session_file = tmp_path / "session.jsonl"
    commands: list[list[str]] = []

    def fake_popen(*args: object, **kwargs: object) -> object:
        commands.append(cast(list[str], args[0]))
        return FakeProcess()

    monkeypatch.setattr(client_module.subprocess, "Popen", fake_popen)

    def fake_rpc_request(command: str) -> dict[str, object]:
        return {
            "type": "response",
            "command": command,
            "data": {"sessionId": "ses_start", "sessionFile": str(session_file)},
        }

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)

    runtime.start(
        env={"JRI_PI_PACKAGE": str(package_root), "JRI_CHAT_RUNTIME": "1"}, cwd=tmp_path, extra_args=["--debug"]
    )

    assert runtime._session_id == "ses_start"
    assert runtime._session_file == session_file
    assert "--extension" in commands[0]
    assert "--skill" in commands[0]
    assert commands[0][-1] == "--debug"


def test_pi_runtime_start_handles_no_cwd_and_package_without_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PiRuntime(binary="pi")
    package_root = tmp_path / "package"
    package_root.mkdir()
    commands: list[list[str]] = []

    def fake_popen(*args: object, **kwargs: object) -> object:
        commands.append(cast(list[str], args[0]))
        assert kwargs["cwd"] is None
        return FakeProcess()

    monkeypatch.setattr(client_module.subprocess, "Popen", fake_popen)

    def fake_rpc_request(command: str) -> dict[str, object]:
        return {"type": "response", "command": command, "data": {}}

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)

    runtime.start(env={"JRI_PI_PACKAGE": str(package_root)}, cwd=None)

    assert "--session-dir" not in commands[0]
    assert "--extension" in commands[0]
    assert "--skill" not in commands[0]


def test_pi_runtime_list_sessions_inserts_healthy_runtime_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    session_file = write_session_file(repo, "ses_saved")
    live_session_file = tmp_path / "live.jsonl"
    live_session_file.write_text('{"type":"session"}\n', encoding="utf-8")
    runtime = PiRuntime(binary="pi")
    runtime._process = cast(Any, FakeProcess())

    def fake_rpc_request(command: str) -> dict[str, object]:
        assert command == "get_state"
        return {
            "type": "response",
            "command": "get_state",
            "data": {"sessionId": "ses_live", "sessionFile": str(live_session_file)},
        }

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)

    sessions = runtime.list_sessions(root=repo)

    assert sessions == [
        {"id": "ses_live", "directory": str(repo.resolve()), "sessionFile": str(live_session_file)},
        {"id": "ses_saved", "directory": str(repo), "sessionFile": str(session_file)},
    ]
    assert runtime._listed_session_files == {"ses_live": live_session_file, "ses_saved": session_file}


def test_pi_runtime_list_sessions_ignores_malformed_headers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    session_dir = repo / ".jri" / "logs" / "chat"
    session_dir.mkdir(parents=True)
    (session_dir / "empty.jsonl").write_text("", encoding="utf-8")
    (session_dir / "bad-json.jsonl").write_text("{\n", encoding="utf-8")
    (session_dir / "array.jsonl").write_text("[]\n", encoding="utf-8")
    (session_dir / "missing-id.jsonl").write_text(json.dumps({"cwd": str(repo)}) + "\n", encoding="utf-8")
    (session_dir / "missing-cwd.jsonl").write_text(json.dumps({"id": "ses_missing_cwd"}) + "\n", encoding="utf-8")
    (session_dir / "other-repo.jsonl").write_text(
        json.dumps({"id": "ses_other", "cwd": str(tmp_path / "other")}) + "\n", encoding="utf-8"
    )
    valid_file = write_session_file(repo, "ses_valid")

    sessions = PiRuntime(binary="pi").list_sessions(root=repo)

    assert sessions == [{"id": "ses_valid", "directory": str(repo), "sessionFile": str(valid_file)}]


def test_pi_runtime_list_sessions_ignores_header_with_non_string_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_read_pi_session_header(session_file: Path) -> dict[str, object]:
        return {"id": "ses_bad", "directory": 123, "sessionFile": str(session_file)}

    monkeypatch.setattr(client_module, "_read_pi_session_header", fake_read_pi_session_header)
    session_dir = repo / ".jri" / "logs" / "chat"
    session_dir.mkdir(parents=True)
    (session_dir / "bad.jsonl").write_text("{}\n", encoding="utf-8")

    assert PiRuntime(binary="pi").list_sessions(root=repo) == []


def test_pi_runtime_list_sessions_ignores_malformed_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    session_dir = repo / ".jri" / "logs" / "chat"
    session_dir.mkdir(parents=True)
    bad_directory_file = session_dir / "bad-directory.jsonl"
    bad_directory_file.write_text(json.dumps({"id": "ses_bad", "cwd": str(repo / "missing")}) + "\n", encoding="utf-8")
    valid_file = write_session_file(repo, "ses_valid")
    original_path = client_module.Path

    def flaky_path(value: str) -> Path:
        if value.endswith("missing"):
            raise OSError("cannot resolve directory")
        return original_path(value)

    monkeypatch.setattr(client_module, "Path", flaky_path)

    sessions = PiRuntime(binary="pi").list_sessions(root=repo)

    assert sessions == [{"id": "ses_valid", "directory": str(repo), "sessionFile": str(valid_file)}]


def test_pi_runtime_list_sessions_ignores_unhealthy_live_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = PiRuntime(binary="pi")
    runtime._process = cast(Any, FakeProcess())

    def fake_rpc_request(command: str) -> dict[str, object]:
        assert command == "get_state"
        return {"type": "response", "command": command, "data": {}}

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)

    assert runtime.list_sessions(root=tmp_path) == []


def test_pi_runtime_list_sessions_ignores_non_object_live_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PiRuntime(binary="pi")
    runtime._process = cast(Any, FakeProcess())

    def fake_rpc_request(command: str) -> dict[str, object]:
        return {"type": "response", "command": command, "data": "bad"}

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)

    assert runtime.list_sessions(root=tmp_path) == []


def test_pi_runtime_list_sessions_tracks_live_state_without_session_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PiRuntime(binary="pi")
    runtime._process = cast(Any, FakeProcess())

    def fake_rpc_request(command: str) -> dict[str, object]:
        return {"type": "response", "command": command, "data": {"sessionId": "ses_live"}}

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)

    assert runtime.list_sessions(root=tmp_path) == [
        {"id": "ses_live", "directory": str(tmp_path.resolve()), "sessionFile": None}
    ]
    assert runtime._listed_session_files == {}


def test_pi_runtime_export_session_uses_listed_session_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    session_file = write_session_file(repo, "ses_saved")
    runtime = PiRuntime(binary="pi")
    runtime.list_sessions(root=repo)
    destination = tmp_path / "exports" / "ses_saved.jsonl"

    runtime.export_session("ses_saved", destination)

    assert destination.read_text(encoding="utf-8") == session_file.read_text(encoding="utf-8")


def test_pi_runtime_export_session_rejects_unavailable_file(tmp_path: Path) -> None:
    session_file = tmp_path / "missing.jsonl"
    runtime = PiRuntime(binary="pi")
    runtime._listed_session_files["ses_missing"] = session_file

    with pytest.raises(JriError, match="session file is unavailable"):
        runtime.export_session("ses_missing", tmp_path / "export.jsonl")


def test_pi_runtime_run_ralph_task_returns_timeout_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = PiRuntime(binary="pi")
    stops: list[int] = []

    def fake_start(*, env: dict[str, str] | None = None, cwd: Path | None = None) -> None:
        del env
        assert cwd == tmp_path
        runtime._process = cast(Any, FakeProcess())
        runtime._session_id = "ses_timeout"

    def fake_stop() -> None:
        stops.append(1)
        runtime._process = None

    monkeypatch.setattr(runtime, "start", fake_start)
    monkeypatch.setattr(runtime, "stop", fake_stop)

    def fake_rpc_request(command: str, extra: dict[str, object] | None = None) -> dict[str, object]:
        assert extra == {"message": "/ralph do task"}
        return {"type": "response", "command": command, "success": True}

    def fake_read_rpc_line(*, timeout: float) -> None:
        assert timeout == 0.5
        return None

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)
    monkeypatch.setattr(runtime, "_read_rpc_line", fake_read_rpc_line)
    ticks = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr("jri.core.agents.client.time.monotonic", lambda: next(ticks))

    result = runtime.run_ralph_task(
        root=tmp_path,
        prompt="do task",
        log_path=tmp_path / "logs" / "ralph.log",
        result_path=tmp_path / "result.json",
        timeout=1,
    )

    assert result.returncode == -1
    assert result.session_id == "ses_timeout"
    assert result.result == "timeout"
    assert result.warnings == ["pi prompt killed after 1s timeout"]
    assert stops == [1]


def test_pi_runtime_does_not_stall_during_active_tool_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PiRuntime(binary="pi")

    def fake_start(*, env: dict[str, str] | None = None, cwd: Path | None = None) -> None:
        del env
        assert cwd == tmp_path
        runtime._process = cast(Any, FakeProcess())
        runtime._session_id = "ses_tool"

    monkeypatch.setattr(runtime, "start", fake_start)
    monkeypatch.setattr(runtime, "stop", lambda: None)

    def fake_rpc_request(command: str, extra: dict[str, object] | None = None) -> dict[str, object]:
        assert extra == {"message": "/ralph do task"}
        return {"type": "response", "command": command, "success": True}

    events: list[dict[str, object] | None] = [
        {"type": "tool_execution_start", "toolCallId": "call_1", "toolName": "ralph-validator"},
        None,
        {"type": "tool_execution_end", "toolCallId": "call_1", "output": "PASS"},
        {"type": "agent_end"},
    ]

    def fake_read_rpc_line(*, timeout: float) -> dict[str, object] | None:
        assert timeout == 0.5
        return events.pop(0)

    result_path = tmp_path / "result.json"

    def fake_monotonic() -> float:
        if not events:
            result_path.write_text('{"result":"completed"}', encoding="utf-8")
        return 0.0 if len(events) >= 3 else 301.0

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)
    monkeypatch.setattr(runtime, "_read_rpc_line", fake_read_rpc_line)
    monkeypatch.setattr("jri.core.agents.client._RUN_STALL_TIMEOUT", 300.0)
    monkeypatch.setattr("jri.core.agents.client.time.monotonic", fake_monotonic)

    result = runtime.run_ralph_task(
        root=tmp_path, prompt="do task", log_path=tmp_path / "logs" / "ralph.log", result_path=result_path
    )

    assert result.result == "completed"
    assert result.warnings == []


def test_pi_runtime_run_ralph_task_reports_prompt_start_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PiRuntime(binary="pi")

    def fake_start(*, env: dict[str, str] | None = None, cwd: Path | None = None) -> None:
        del env, cwd
        runtime._process = cast(Any, FakeProcess())

    monkeypatch.setattr(runtime, "start", fake_start)

    def fake_rpc_request(command: str, extra: dict[str, object] | None = None) -> dict[str, object]:
        del extra
        return {"type": "response", "command": command, "success": False}

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)

    with pytest.raises(JriError, match="failed to start ralph prompt"):
        runtime.run_ralph_task(
            root=tmp_path, prompt="do task", log_path=tmp_path / "ralph.log", result_path=tmp_path / "result.json"
        )


def test_pi_runtime_run_ralph_task_reports_missing_process_after_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PiRuntime(binary="pi")

    def fake_start(*, env: dict[str, str] | None = None, cwd: Path | None = None) -> None:
        del env, cwd
        runtime._process = None

    monkeypatch.setattr(runtime, "start", fake_start)

    with pytest.raises(JriError, match="pi rpc process is not running"):
        runtime.run_ralph_task(
            root=tmp_path, prompt="do task", log_path=tmp_path / "ralph.log", result_path=tmp_path / "result.json"
        )


def test_pi_runtime_run_ralph_task_writes_logs_calls_on_start_and_reads_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runtime = PiRuntime(binary="pi")
    result_path = tmp_path / "result.json"
    result_path.write_text("old", encoding="utf-8")
    started_pids: list[int] = []

    def fake_start(*, env: dict[str, str] | None = None, cwd: Path | None = None) -> None:
        del env
        assert cwd == tmp_path
        runtime._process = cast(Any, FakeProcess())
        runtime._session_id = "ses_done"

    monkeypatch.setattr(runtime, "start", fake_start)

    def fake_rpc_request(command: str, extra: dict[str, object] | None = None) -> dict[str, object]:
        del extra
        return {"type": "response", "command": command, "success": True}

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)
    events: list[dict[str, object]] = [
        {"type": "message_update", "text": "hello"},
        {
            "type": "tool_execution_start",
            "toolCallId": "call-1",
            "toolName": "read",
            "input": {"filePath": str(tmp_path / "file.py")},
        },
        {"type": "heartbeat"},
        {"type": "agent_end"},
    ]

    def fake_read_rpc_line(*, timeout: float) -> dict[str, object]:
        del timeout
        event = events.pop(0)
        if event["type"] == "agent_end":
            result_path.write_text('{"result":"completed"}', encoding="utf-8")
        return event

    monkeypatch.setattr(runtime, "_read_rpc_line", fake_read_rpc_line)
    monkeypatch.setattr(client_module.time, "monotonic", lambda: 0.0)

    result = runtime.run_ralph_task(
        root=tmp_path,
        prompt="do task",
        log_path=tmp_path / "logs" / "ralph.log",
        result_path=result_path,
        on_start=started_pids.append,
    )

    assert started_pids == [4321]
    assert result.result == "completed"
    assert result.session_id == "ses_done"
    assert "old" not in result_path.read_text(encoding="utf-8")
    assert "message_update" in (tmp_path / "logs" / "ralph.log").read_text(encoding="utf-8")
    assert "hello\n" in capsys.readouterr().out


def test_pi_runtime_run_ralph_task_requests_missing_payload_follow_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PiRuntime(binary="pi")
    prompts: list[str] = []
    result_path = tmp_path / "result.json"

    def fake_start(*, env: dict[str, str] | None = None, cwd: Path | None = None) -> None:
        del env, cwd
        runtime._process = cast(Any, FakeProcess())

    def fake_rpc_request(command: str, extra: dict[str, object] | None = None) -> dict[str, object]:
        assert command == "prompt"
        assert extra is not None
        prompts.append(cast(str, extra["message"]))
        return {"type": "response", "command": command, "success": True}

    events: list[dict[str, object]] = [{"type": "agent_end"}, {"type": "agent_end"}]

    def fake_read_rpc_line(*, timeout: float) -> dict[str, object]:
        del timeout
        event = events.pop(0)
        if len(events) == 0:
            result_path.write_text('{"result":"completed"}', encoding="utf-8")
        return event

    monkeypatch.setattr(runtime, "start", fake_start)
    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)
    monkeypatch.setattr(runtime, "_read_rpc_line", fake_read_rpc_line)
    monkeypatch.setattr(client_module.time, "monotonic", lambda: 0.0)

    result = runtime.run_ralph_task(
        root=tmp_path, prompt="do task", log_path=tmp_path / "ralph.log", result_path=result_path
    )

    assert result.result == "completed"
    assert prompts == ["/ralph do task", client_module._MISSING_RESULT_FOLLOW_UP_PROMPT]


def test_pi_runtime_run_ralph_task_handles_failed_follow_up_stall_and_missing_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def prepare_runtime() -> PiRuntime:
        runtime = PiRuntime(binary="pi")

        def fake_start(*, env: dict[str, str] | None = None, cwd: Path | None = None) -> None:
            del env, cwd
            runtime._process = cast(Any, FakeProcess())

        monkeypatch.setattr(runtime, "start", fake_start)
        return runtime

    runtime = prepare_runtime()
    calls = 0

    def failed_follow_up_rpc(command: str, extra: dict[str, object] | None = None) -> dict[str, object]:
        nonlocal calls
        del extra
        calls += 1
        return {"type": "response", "command": command, "success": calls == 1}

    monkeypatch.setattr(runtime, "_rpc_request", failed_follow_up_rpc)

    def fake_read_rpc_line(*, timeout: float) -> dict[str, object]:
        del timeout
        return {"type": "agent_end"}

    monkeypatch.setattr(runtime, "_read_rpc_line", fake_read_rpc_line)
    monkeypatch.setattr(client_module.time, "monotonic", lambda: 0.0)
    result = runtime.run_ralph_task(
        root=tmp_path, prompt="do task", log_path=tmp_path / "follow-up.log", result_path=tmp_path / "follow-up.json"
    )
    assert result.result == "failed"
    assert result.warnings == ["missing result payload for Ralph run; treating run as failed"]

    runtime = prepare_runtime()
    stops: list[int] = []
    monkeypatch.setattr(runtime, "stop", lambda: stops.append(1))

    def fake_rpc_request(command: str, extra: dict[str, object] | None = None) -> dict[str, object]:
        del extra
        return {"type": "response", "command": command, "success": True}

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)

    def fake_read_rpc_line_none(*, timeout: float) -> None:
        del timeout

    monkeypatch.setattr(runtime, "_read_rpc_line", fake_read_rpc_line_none)
    ticks = iter([0.0, 301.0])
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(ticks))
    result = runtime.run_ralph_task(
        root=tmp_path, prompt="do task", log_path=tmp_path / "stall.log", result_path=tmp_path / "stall.json"
    )
    assert result.result == "failed"
    assert "stalled" in result.warnings[0]
    assert stops == [1]


def test_pi_runtime_rpc_request_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = PiRuntime(binary="pi")
    process = FakeProcess()
    runtime._process = cast(Any, process)
    ticks = iter([0.0, 31.0])
    monkeypatch.setattr("jri.core.agents.client.time.monotonic", lambda: next(ticks))

    with pytest.raises(JriError, match="rpc command 'prompt' timed out"):
        runtime._rpc_request("prompt", {"message": "hello"})

    assert json.loads(process.stdin.getvalue()) == {"type": "prompt", "message": "hello"}


def test_pi_runtime_rpc_request_ignores_non_matching_events(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = PiRuntime(binary="pi")
    runtime._process = cast(Any, FakeProcess())
    events: list[dict[str, object] | None] = [
        None,
        {"type": "response", "command": "other"},
        {"type": "response", "command": "prompt", "success": True},
    ]
    ticks = iter([0.0, 1.0, 2.0, 3.0])
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(ticks))

    def fake_read_rpc_line(*, timeout: float) -> dict[str, object] | None:
        del timeout
        return events.pop(0)

    monkeypatch.setattr(runtime, "_read_rpc_line", fake_read_rpc_line)

    assert runtime._rpc_request("prompt") == {"type": "response", "command": "prompt", "success": True}


def test_pi_runtime_rpc_helpers_report_missing_streams() -> None:
    runtime = PiRuntime(binary="pi")

    with pytest.raises(JriError, match="pi rpc process is not running"):
        runtime._write_rpc({"type": "prompt"})
    with pytest.raises(JriError, match="pi rpc process is not running"):
        runtime._read_rpc_line(timeout=0)

    process = FakeProcess()
    process.stdin = cast(Any, None)
    runtime._process = cast(Any, process)

    with pytest.raises(JriError, match="pi rpc process is not running"):
        runtime._write_rpc({"type": "prompt"})

    process = FakeProcess()
    process.stdout = cast(Any, None)
    runtime._process = cast(Any, process)

    with pytest.raises(JriError, match="pi rpc process is not running"):
        runtime._read_rpc_line(timeout=0)


def test_pi_runtime_read_rpc_line_handles_raw_text_and_process_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = PiRuntime(binary="pi")
    process = FakeProcess()
    process.stdout = io.StringIO("not json\n")
    runtime._process = cast(Any, process)

    assert runtime._read_rpc_line(timeout=0) == {"type": "raw", "text": "not json"}

    runtime._process = cast(Any, FakeProcess(poll_result=1))

    def fake_readline_with_timeout(stream: io.StringIO, *, timeout: float) -> None:
        assert stream is cast(Any, runtime._process).stdout
        assert timeout == 0
        return None

    monkeypatch.setattr("jri.core.agents.client._readline_with_timeout", fake_readline_with_timeout)

    with pytest.raises(JriError, match="pi rpc process exited unexpectedly"):
        runtime._read_rpc_line(timeout=0)


def test_pi_runtime_read_rpc_line_ignores_non_object_json() -> None:
    runtime = PiRuntime(binary="pi")
    process = FakeProcess()
    process.stdout = io.StringIO("[]\n")
    runtime._process = cast(Any, process)

    assert runtime._read_rpc_line(timeout=0) is None


def test_pi_runtime_read_rpc_line_returns_none_while_process_is_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = PiRuntime(binary="pi")
    runtime._process = cast(Any, FakeProcess())

    def fake_readline_with_timeout(stream: io.StringIO, *, timeout: float) -> None:
        del stream, timeout

    monkeypatch.setattr(client_module, "_readline_with_timeout", fake_readline_with_timeout)

    assert runtime._read_rpc_line(timeout=0.1) is None


def test_readline_with_timeout_reports_timeout_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO("line\n")

    class FakeSelect:
        @staticmethod
        def select(
            read_list: list[io.StringIO], write_list: list[object], error_list: list[object], timeout: float
        ) -> tuple[list[io.StringIO], list[object], list[object]]:
            del write_list, error_list
            assert read_list == [stream]
            assert timeout == 0.1
            return [], [], []

    monkeypatch.setitem(sys.modules, "select", FakeSelect)
    assert _readline_with_timeout(stream, timeout=0.1) is None

    class ReadySelect:
        @staticmethod
        def select(
            read_list: list[io.StringIO], write_list: list[object], error_list: list[object], timeout: float
        ) -> tuple[list[io.StringIO], list[object], list[object]]:
            del write_list, error_list, timeout
            return read_list, [], []

    monkeypatch.setitem(sys.modules, "select", ReadySelect)
    assert _readline_with_timeout(stream, timeout=0.1) == "line\n"

    class RaisingSelect:
        @staticmethod
        def select(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise OSError("select unavailable")

    monkeypatch.setitem(sys.modules, "select", RaisingSelect)
    fallback_stream = io.StringIO("fallback\n")
    assert _readline_with_timeout(fallback_stream, timeout=0.1) == "fallback\n"


def test_readline_with_timeout_reads_directly_on_non_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO("windows\n")
    monkeypatch.setattr(client_module.os, "name", "nt")

    assert _readline_with_timeout(stream, timeout=0.1) == "windows\n"


def test_pi_runtime_compile_intent_graph_prompts_read_only_and_parses_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PiRuntime(binary="pi")
    prompts: list[dict[str, object] | None] = []

    def fake_start(
        *, env: dict[str, str] | None = None, cwd: Path | None = None, extra_args: list[str] | None = None
    ) -> None:
        assert env is None
        assert cwd == tmp_path
        assert extra_args is not None
        assert "--no-extensions" in extra_args
        assert "--no-skills" in extra_args
        assert "--no-prompt-templates" in extra_args
        assert "--no-context-files" in extra_args
        tools_index = extra_args.index("--tools")
        assert extra_args[tools_index + 1] == "read,grep,find,ls"
        prompt_index = extra_args.index("--append-system-prompt")
        assert extra_args[prompt_index + 1].endswith("compiler/prompt.md")
        runtime._process = cast(Any, FakeProcess())
        runtime._session_id = "ses_compiler"

    def fake_rpc_request(command: str, extra: dict[str, object] | None = None) -> dict[str, object]:
        assert command == "prompt"
        prompts.append(extra)
        return {"type": "response", "command": command, "success": True}

    events: list[dict[str, object]] = [{"type": "message_update", "text": '{"tasks": []}'}, {"type": "agent_end"}]

    def fake_read_rpc_line(*, timeout: float) -> dict[str, object]:
        assert timeout == 0.5
        return events.pop(0)

    monkeypatch.setattr(runtime, "start", fake_start)
    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)
    monkeypatch.setattr(runtime, "_read_rpc_line", fake_read_rpc_line)

    result = runtime.compile_intent_graph(root=tmp_path, context={"changed_paths": ["product"], "graph_nodes": []})

    assert result == {"tasks": []}
    assert prompts and prompts[0] is not None
    message = str(prompts[0]["message"])
    assert message.startswith("Context:\n")
    assert "product" in message
    assert "Return only JSON" not in message


def test_pi_runtime_compile_intent_graph_rejects_non_json_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PiRuntime(binary="pi")

    def fake_start(
        *, env: dict[str, str] | None = None, cwd: Path | None = None, extra_args: list[str] | None = None
    ) -> None:
        del env
        assert cwd == tmp_path
        assert extra_args is not None
        assert extra_args[-2] == "--append-system-prompt"
        assert extra_args[-1].endswith("compiler/prompt.md")
        runtime._process = cast(Any, FakeProcess())

    monkeypatch.setattr(runtime, "start", fake_start)

    def fake_rpc_request(command: str, extra: dict[str, object] | None = None) -> dict[str, object]:
        del extra
        return {"type": "response", "command": command, "success": True}

    events: list[dict[str, object]] = [{"type": "message_update", "text": "not json"}, {"type": "agent_end"}]

    def fake_read_rpc_line(*, timeout: float) -> dict[str, object]:
        del timeout
        return events.pop(0)

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)
    monkeypatch.setattr(runtime, "_read_rpc_line", fake_read_rpc_line)

    with pytest.raises(JriError, match="compiler did not return valid JSON"):
        runtime.compile_intent_graph(root=tmp_path, context={})


def test_compiler_event_text_extracts_public_message_shapes() -> None:
    assert _compiler_event_text({"type": "message.part.delta", "properties": {"field": "text", "delta": "a"}}) == "a"
    assert _compiler_event_text({"type": "message.part.delta", "properties": {"field": "text", "delta": 1}}) == ""
    assert _compiler_event_text({"type": "message.part.delta", "properties": {"field": "thinking"}}) == ""
    assert (
        _compiler_event_text({
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "b"},
                    {"type": "image", "text": "ignored"},
                    "ignored",
                    {"type": "text", "text": "c"},
                ],
            },
        })
        == "bc"
    )
    assert _compiler_event_text({"type": "assistant", "assistantMessageEvent": {"content": "d"}}) == "d"
    assert _compiler_event_text({"type": "assistant", "message": {"role": "user"}}) == ""
    assert _message_content_text({"type": "text", "text": "ignored"}) == ""


def test_pi_runtime_compile_intent_graph_stops_healthy_process_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PiRuntime(binary="pi")
    runtime._process = cast(Any, FakeProcess())
    stops: list[int] = []

    def fake_stop() -> None:
        stops.append(1)
        runtime._process = None

    def fake_start(
        *, env: dict[str, str] | None = None, cwd: Path | None = None, extra_args: list[str] | None = None
    ) -> None:
        del env, extra_args
        assert cwd == tmp_path
        runtime._process = cast(Any, FakeProcess())

    monkeypatch.setattr(runtime, "stop", fake_stop)
    monkeypatch.setattr(runtime, "start", fake_start)

    def fake_rpc_request(command: str, extra: dict[str, object] | None = None) -> dict[str, object]:
        del extra
        return {"type": "response", "command": command, "success": True}

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)
    events: list[dict[str, object]] = [{"type": "agent_end"}]

    def fake_read_rpc_line(*, timeout: float) -> dict[str, object] | None:
        del timeout
        return events.pop(0)

    monkeypatch.setattr(runtime, "_read_rpc_line", fake_read_rpc_line)

    with pytest.raises(JriError, match="compiler did not return valid JSON"):
        runtime.compile_intent_graph(root=tmp_path, context={})

    assert stops == [1]


@pytest.mark.parametrize(
    ("start_sets_process", "response", "expected"),
    [
        (False, {"success": True}, "pi rpc process is not running"),
        (True, {"success": False, "error": "nope"}, "failed to start intent compiler prompt"),
    ],
)
def test_pi_runtime_compile_intent_graph_reports_start_and_prompt_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_sets_process: bool,
    response: dict[str, object],
    expected: str,
) -> None:
    runtime = PiRuntime(binary="pi")

    def fake_start(
        *, env: dict[str, str] | None = None, cwd: Path | None = None, extra_args: list[str] | None = None
    ) -> None:
        del env, cwd, extra_args
        if start_sets_process:
            runtime._process = cast(Any, FakeProcess())

    monkeypatch.setattr(runtime, "start", fake_start)

    def fake_rpc_request(command: str, extra: dict[str, object] | None = None) -> dict[str, object]:
        del extra
        return {"type": "response", "command": command, **response}

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)

    with pytest.raises(JriError, match=expected):
        runtime.compile_intent_graph(root=tmp_path, context={})


def test_pi_runtime_compile_intent_graph_times_out_and_rejects_non_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PiRuntime(binary="pi")
    stops: list[int] = []

    def fake_start(
        *, env: dict[str, str] | None = None, cwd: Path | None = None, extra_args: list[str] | None = None
    ) -> None:
        del env, cwd, extra_args
        runtime._process = cast(Any, FakeProcess())

    monkeypatch.setattr(runtime, "start", fake_start)
    monkeypatch.setattr(runtime, "stop", lambda: stops.append(1))

    def fake_rpc_request(command: str, extra: dict[str, object] | None = None) -> dict[str, object]:
        del extra
        return {"type": "response", "command": command, "success": True}

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)

    def fake_read_rpc_line_none(*, timeout: float) -> None:
        del timeout

    monkeypatch.setattr(runtime, "_read_rpc_line", fake_read_rpc_line_none)
    ticks = iter([0.0, 301.0])
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(ticks))

    with pytest.raises(JriError, match="intent compiler timed out"):
        runtime.compile_intent_graph(root=tmp_path, context={})
    assert stops == [1]

    runtime = PiRuntime(binary="pi")

    def fake_start_non_object(
        *, env: dict[str, str] | None = None, cwd: Path | None = None, extra_args: list[str] | None = None
    ) -> None:
        del env, cwd, extra_args
        runtime._process = cast(Any, FakeProcess())

    monkeypatch.setattr(runtime, "start", fake_start_non_object)

    def fake_rpc_request_non_object(command: str, extra: dict[str, object] | None = None) -> dict[str, object]:
        del extra
        return {"type": "response", "command": command, "success": True}

    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request_non_object)
    events: list[dict[str, object] | None] = [None, {"type": "message_update", "text": "[]"}, {"type": "agent_end"}]

    def fake_read_rpc_line(*, timeout: float) -> dict[str, object] | None:
        del timeout
        return events.pop(0)

    monkeypatch.setattr(runtime, "_read_rpc_line", fake_read_rpc_line)
    monkeypatch.setattr(client_module.time, "monotonic", lambda: 0.0)

    with pytest.raises(JriError, match="compiler JSON output must be an object"):
        runtime.compile_intent_graph(root=tmp_path, context={})
