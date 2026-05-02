import io
import json
import signal
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from jri.core.agents.client import PiRuntime
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
            raise subprocess.TimeoutExpired(
                cmd="pi", timeout=0 if timeout is None else timeout
            )
        return 0


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


def test_pi_runtime_stop_returns_when_no_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = PiRuntime(binary="pi")

    def fail_getpgid(pid: int) -> int:
        del pid
        raise AssertionError("no process should not query a process group")

    monkeypatch.setattr("jri.core.agents.client.os.getpgid", fail_getpgid)

    runtime.stop()

    assert runtime._process is None


def test_pi_runtime_stop_drops_already_exited_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_pi_runtime_stop_terminates_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = PiRuntime(binary="pi")
    process = FakeProcess()
    runtime._process = cast(Any, process)
    signals: list[tuple[int, signal.Signals]] = []

    def fake_getpgid(pid: int) -> int:
        assert pid == process.pid
        return 9876

    monkeypatch.setattr("jri.core.agents.client.os.getpgid", fake_getpgid)
    monkeypatch.setattr(
        "jri.core.agents.client.os.killpg",
        lambda pgid, sig: signals.append((pgid, cast(signal.Signals, sig))),
    )

    runtime.stop()

    assert runtime._process is None
    assert signals == [(9876, signal.SIGTERM)]
    assert process.wait_timeouts == [5]
    assert process.terminated is False
    assert process.killed is False


def test_pi_runtime_stop_kills_process_after_terminate_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        {
            "id": "ses_live",
            "directory": str(repo.resolve()),
            "sessionFile": str(live_session_file),
        },
        {
            "id": "ses_saved",
            "directory": str(repo),
            "sessionFile": str(session_file),
        },
    ]
    assert runtime._listed_session_files == {
        "ses_live": live_session_file,
        "ses_saved": session_file,
    }


def test_pi_runtime_list_sessions_ignores_malformed_headers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    session_dir = repo / ".jri" / "logs" / "chat"
    session_dir.mkdir(parents=True)
    (session_dir / "empty.jsonl").write_text("", encoding="utf-8")
    (session_dir / "bad-json.jsonl").write_text("{\n", encoding="utf-8")
    (session_dir / "array.jsonl").write_text("[]\n", encoding="utf-8")
    (session_dir / "missing-id.jsonl").write_text(
        json.dumps({"cwd": str(repo)}) + "\n", encoding="utf-8"
    )
    (session_dir / "missing-cwd.jsonl").write_text(
        json.dumps({"id": "ses_missing_cwd"}) + "\n", encoding="utf-8"
    )
    (session_dir / "other-repo.jsonl").write_text(
        json.dumps({"id": "ses_other", "cwd": str(tmp_path / "other")}) + "\n",
        encoding="utf-8",
    )
    valid_file = write_session_file(repo, "ses_valid")

    sessions = PiRuntime(binary="pi").list_sessions(root=repo)

    assert sessions == [
        {"id": "ses_valid", "directory": str(repo), "sessionFile": str(valid_file)}
    ]


def test_pi_runtime_export_session_uses_listed_session_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    session_file = write_session_file(repo, "ses_saved")
    runtime = PiRuntime(binary="pi")
    runtime.list_sessions(root=repo)
    destination = tmp_path / "exports" / "ses_saved.jsonl"

    runtime.export_session("ses_saved", destination)

    assert destination.read_text(encoding="utf-8") == session_file.read_text(
        encoding="utf-8"
    )


def test_pi_runtime_export_session_rejects_unavailable_file(tmp_path: Path) -> None:
    session_file = tmp_path / "missing.jsonl"
    runtime = PiRuntime(binary="pi")
    runtime._listed_session_files["ses_missing"] = session_file

    with pytest.raises(JriError, match="session file is unavailable"):
        runtime.export_session("ses_missing", tmp_path / "export.jsonl")


def test_pi_runtime_run_ralph_task_returns_timeout_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PiRuntime(binary="pi")
    stops: list[int] = []

    def fake_start(
        *, env: dict[str, str] | None = None, cwd: Path | None = None
    ) -> None:
        del env
        assert cwd == tmp_path
        runtime._process = cast(Any, FakeProcess())
        runtime._session_id = "ses_timeout"

    def fake_stop() -> None:
        stops.append(1)
        runtime._process = None

    monkeypatch.setattr(runtime, "start", fake_start)
    monkeypatch.setattr(runtime, "stop", fake_stop)

    def fake_rpc_request(
        command: str, extra: dict[str, object] | None = None
    ) -> dict[str, object]:
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


def test_pi_runtime_rpc_request_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = PiRuntime(binary="pi")
    process = FakeProcess()
    runtime._process = cast(Any, process)
    ticks = iter([0.0, 31.0])
    monkeypatch.setattr("jri.core.agents.client.time.monotonic", lambda: next(ticks))

    with pytest.raises(JriError, match="rpc command 'prompt' timed out"):
        runtime._rpc_request("prompt", {"message": "hello"})

    assert json.loads(process.stdin.getvalue()) == {
        "type": "prompt",
        "message": "hello",
    }


def test_pi_runtime_read_rpc_line_handles_raw_text_and_process_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(
        "jri.core.agents.client._readline_with_timeout", fake_readline_with_timeout
    )

    with pytest.raises(JriError, match="pi rpc process exited unexpectedly"):
        runtime._read_rpc_line(timeout=0)
