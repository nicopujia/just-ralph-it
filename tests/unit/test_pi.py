import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from jri.core.agents import (
    PiRuntime,
    _missing_result_payload,
    _parse_event_line,
    _parse_result_payload,
    launch_chat,
    render_saved_log,
)
from jri.core.errors import JriError


def _result_payload(result: str = "completed") -> str:
    return json.dumps({"result": result}) + "\n"


def test_render_saved_log_replays_streamed_text_and_tool_labels() -> None:
    text = "\n".join(
        [
            json.dumps({"type": "message_update", "delta": "hello"}),
            json.dumps(
                {
                    "type": "tool_execution_start",
                    "toolCallId": "call_1",
                    "toolName": "read",
                    "input": {"path": "/repo/file.txt"},
                }
            ),
            json.dumps({"type": "message_update", "delta": "done"}),
        ]
    )

    rendered = render_saved_log(text, cwd_hint="/repo/")

    assert "hello" in rendered
    assert "read file.txt" in rendered
    assert "done" in rendered


def test_parse_event_line_extracts_terminal_text_from_message_update() -> None:
    _, terminal_text, is_tool = _parse_event_line(
        json.dumps({"type": "message_update", "delta": "hello"}) + "\n"
    )

    assert terminal_text == "hello"
    assert is_tool is False


def test_parse_event_line_extracts_tool_output() -> None:
    _, terminal_text, is_tool = _parse_event_line(
        json.dumps(
            {
                "type": "tool_execution_end",
                "toolName": "read",
                "output": "line 1\nline 2",
            }
        )
        + "\n"
    )

    assert terminal_text == "line 1\nline 2"
    assert is_tool is True


def test_parse_event_line_preserves_plain_text_fallback() -> None:
    payload, terminal_text, is_tool = _parse_event_line("plain text\n")

    assert payload is None
    assert terminal_text == "plain text\n"
    assert is_tool is False


def test_missing_result_payload_reports_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result, warnings = _missing_result_payload(context="Ralph run")

    assert result == "failed"
    assert warnings == ["missing result payload for Ralph run; treating run as failed"]
    assert warnings[0] in capsys.readouterr().err


def test_parse_result_payload_accepts_completed() -> None:
    payload, warnings = _parse_result_payload(_result_payload("completed"))

    assert warnings == []
    assert payload is not None
    assert payload.result == "completed"


def test_parse_result_payload_validates_needs_human() -> None:
    payload, warnings = _parse_result_payload(
        json.dumps(
            {
                "result": "needs_human",
                "blocker": "missing secret",
                "human_task": {
                    "title": "Provide secret",
                    "body": "Add the production secret.",
                    "acceptance_criteria": ["Secret is available"],
                },
            }
        )
    )

    assert warnings == []
    assert payload is not None
    assert payload.result == "needs_human"
    assert payload.human_task is not None


def test_parse_result_payload_rejects_human_task_slug() -> None:
    payload, warnings = _parse_result_payload(
        json.dumps(
            {
                "result": "needs_human",
                "blocker": "missing secret",
                "human_task": {
                    "slug": "provide-secret",
                    "title": "Provide secret",
                    "body": "Add the production secret.",
                    "acceptance_criteria": ["Secret is available"],
                },
            }
        )
    )

    assert payload is None
    assert warnings == [
        "invalid result payload; treating run as failed: "
        "`human_task.slug` is not supported; JRI derives the Human task slug"
    ]


def test_pi_runtime_rpc_request_reads_matching_response() -> None:
    runtime = PiRuntime(binary="pi")
    stdin = io.StringIO()
    stdout = io.StringIO('{"type":"response","command":"get_state","success":true}\n')

    process = SimpleNamespace(
        pid=123,
        stdin=stdin,
        stdout=stdout,
        poll=lambda: None,
    )
    runtime._process = cast(Any, process)

    response = runtime._rpc_request("get_state")

    assert response["success"] is True
    assert json.loads(stdin.getvalue()) == {"type": "get_state"}


def test_pi_runtime_start_appends_ralph_prompt_and_loads_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "package"
    (package_root / "extensions").mkdir(parents=True)
    (package_root / "prompts").mkdir()
    (package_root / "skills" / "hosted-projects").mkdir(parents=True)
    (package_root / "skills" / "reverse-ralph").mkdir()
    (package_root / "extensions" / "jri.ts").write_text("", encoding="utf-8")
    (package_root / "prompts" / "ralph.md").write_text("", encoding="utf-8")

    popen_calls: list[list[str]] = []
    popen_envs: list[dict[str, str]] = []

    def fake_popen(*args: object, **kwargs: object) -> object:
        popen_calls.append(cast(list[str], args[0]))
        popen_envs.append(cast(dict[str, str], kwargs["env"]))
        return SimpleNamespace(
            pid=123,
            stdin=io.StringIO(),
            stdout=io.StringIO(
                '{"type":"response","command":"get_state","success":true}\n'
            ),
            poll=lambda: None,
        )

    monkeypatch.setattr("jri.core.agents.client.subprocess.Popen", fake_popen)
    monkeypatch.setenv("JRI_CHAT_RUNTIME", "1")

    runtime = PiRuntime(binary="pi")
    runtime.start(
        env={"JRI_PI_PACKAGE": str(package_root), "JRI_CHAT_RUNTIME": "1"},
        cwd=tmp_path,
    )

    assert popen_calls == [
        [
            "pi",
            "--mode",
            "rpc",
            "--extension",
            str(package_root / "extensions" / "jri.ts"),
            "--append-system-prompt",
            str(package_root / "prompts" / "ralph.md"),
            "--skill",
            str(package_root / "skills" / "hosted-projects"),
            "--skill",
            str(package_root / "skills" / "reverse-ralph"),
        ]
    ]
    assert str(package_root / "extensions" / "jri-validator.ts") not in popen_calls[0]
    assert "JRI_CHAT_RUNTIME" not in popen_envs[0]


def test_pi_runtime_uses_fresh_rpc_process_for_each_ralph_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PiRuntime(binary="pi")
    runtime._env = {"JRI_PI_PACKAGE": "package"}
    runtime._process = cast(
        Any,
        SimpleNamespace(
            pid=111,
            stdin=io.StringIO(),
            stdout=io.StringIO(),
            poll=lambda: None,
        ),
    )
    result_path = tmp_path / "result.json"
    starts: list[Path | None] = []
    stops: list[int] = []

    def fake_stop() -> None:
        stops.append(1)
        runtime._process = None

    def fake_start(
        *, env: dict[str, str] | None = None, cwd: Path | None = None
    ) -> None:
        assert env == {"JRI_PI_PACKAGE": "package"}
        starts.append(cwd)
        runtime._process = cast(
            Any,
            SimpleNamespace(
                pid=222,
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                poll=lambda: None,
            ),
        )
        runtime._session_id = "ses_fresh"

    def fake_rpc_request(
        command: str, extra: dict[str, object] | None = None
    ) -> dict[str, object]:
        assert command == "prompt"
        assert extra is not None
        return {"type": "response", "command": command, "success": True}

    def fake_read_rpc_line(*, timeout: float) -> dict[str, object] | None:
        del timeout
        result_path.write_text(_result_payload(), encoding="utf-8")
        return {"type": "agent_end"}

    monkeypatch.setattr(runtime, "stop", fake_stop)
    monkeypatch.setattr(runtime, "start", fake_start)
    monkeypatch.setattr(runtime, "_rpc_request", fake_rpc_request)
    monkeypatch.setattr(runtime, "_read_rpc_line", fake_read_rpc_line)

    result = runtime.run_ralph_task(
        root=tmp_path,
        prompt="do task",
        log_path=tmp_path / "ralph.log",
        result_path=result_path,
    )

    assert stops == [1]
    assert starts == [tmp_path]
    assert result.session_id == "ses_fresh"
    assert result.result == "completed"


def test_launch_chat_appends_interrogator_prompt_and_loads_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "package"
    (package_root / "extensions").mkdir(parents=True)
    (package_root / "prompts").mkdir()
    (package_root / "extensions" / "jri.ts").write_text("", encoding="utf-8")
    (package_root / "extensions" / "jri-validator.ts").write_text("", encoding="utf-8")
    (package_root / "prompts" / "interrogator.md").write_text("", encoding="utf-8")
    run_calls: list[list[str]] = []
    run_envs: list[dict[str, str]] = []

    def fake_run(*args: object, **kwargs: object) -> object:
        run_calls.append(cast(list[str], args[0]))
        run_envs.append(cast(dict[str, str], kwargs["env"]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("jri.core.agents.client.subprocess.run", fake_run)

    assert (
        launch_chat(
            root=tmp_path,
            session_id=None,
            extra_args=[],
            binary="pi",
            env={"JRI_PI_PACKAGE": str(package_root), "JRI_CHAT_RUNTIME": "0"},
        )
        == 0
    )

    assert run_calls == [
        [
            "pi",
            "--extension",
            str(package_root / "extensions" / "jri.ts"),
            "--append-system-prompt",
            str(package_root / "prompts" / "interrogator.md"),
        ]
    ]
    assert str(package_root / "extensions" / "jri-validator.ts") not in run_calls[0]
    assert run_envs[0]["JRI_CHAT_RUNTIME"] == "1"


def test_pi_runtime_export_session_copies_session_file(tmp_path: Path) -> None:
    session_file = tmp_path / "session.jsonl"
    session_file.write_text('{"type":"message"}\n', encoding="utf-8")
    destination = tmp_path / "export.json"
    runtime = PiRuntime(binary="pi")
    runtime._session_id = "ses_123"
    runtime._session_file = session_file

    runtime.export_session("ses_123", destination)

    assert destination.read_text(encoding="utf-8") == '{"type":"message"}\n'


def test_pi_runtime_export_session_rejects_unknown_session(tmp_path: Path) -> None:
    runtime = PiRuntime(binary="pi")
    runtime._session_id = "ses_123"
    runtime._session_file = tmp_path / "session.jsonl"

    with pytest.raises(JriError, match="unknown pi session"):
        runtime.export_session("ses_other", tmp_path / "export.json")
