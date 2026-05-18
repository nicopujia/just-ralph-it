import io
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import jri.core.agents.resources as resources
from jri.core.agents import (
    PiRuntime,
    _missing_result_payload,
    _parse_event_line,
    _parse_result_payload,
    launch_chat,
    render_saved_log,
)
from jri.core.agents.session import (
    _write_package_manifest,
    call_with_runtime,
    detect_latest_session,
    export_session_if_available,
    runtime_env,
)
from jri.core.agents.session import list_sessions as session_list_sessions
from jri.core.errors import JriError
from jri.core.graph import graph_node_path
from jri.core.models import AttemptState, HumanTaskPayload, ProcessState, RalphResultPayload, State
from jri.core.paths import JriPaths
from jri.core.timeline import TimelineEvent, TimelineStore


def _result_payload(result: str = "completed") -> str:
    return json.dumps({"result": result}) + "\n"


def _manifest_files(payload: str) -> object:
    def read_text(**kwargs: object) -> str:
        del kwargs
        return payload

    manifest_file = SimpleNamespace(read_text=read_text)

    def joinpath(name: str) -> object:
        del name
        return manifest_file

    return SimpleNamespace(joinpath=joinpath)


def test_render_saved_log_replays_streamed_text_and_tool_labels() -> None:
    text = "\n".join([
        json.dumps({"type": "message_update", "delta": "hello"}),
        json.dumps({
            "type": "tool_execution_start",
            "toolCallId": "call_1",
            "toolName": "read",
            "input": {"path": "/repo/file.txt"},
        }),
        json.dumps({"type": "message_update", "delta": "done"}),
    ])

    rendered = render_saved_log(text, cwd_hint="/repo/")

    assert "hello" in rendered
    assert "read file.txt" in rendered
    assert "done" in rendered


def test_parse_event_line_extracts_terminal_text_from_message_update() -> None:
    _, terminal_text, is_tool = _parse_event_line(json.dumps({"type": "message_update", "delta": "hello"}) + "\n")

    assert terminal_text == "hello"
    assert is_tool is False


def test_parse_event_line_extracts_tool_output() -> None:
    _, terminal_text, is_tool = _parse_event_line(
        json.dumps({"type": "tool_execution_end", "toolName": "read", "output": "line 1\nline 2"}) + "\n"
    )

    assert terminal_text == "line 1\nline 2"
    assert is_tool is True


def test_parse_event_line_preserves_plain_text_fallback() -> None:
    payload, terminal_text, is_tool = _parse_event_line("plain text\n")

    assert payload is None
    assert terminal_text == "plain text\n"
    assert is_tool is False


def test_missing_result_payload_reports_failed(capsys: pytest.CaptureFixture[str]) -> None:
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
        json.dumps({
            "result": "needs_human",
            "blocker": "missing secret",
            "human_task": {
                "title": "Provide secret",
                "body": "Add the production secret.",
                "acceptance_criteria": ["Secret is available"],
            },
        })
    )

    assert warnings == []
    assert payload is not None
    assert payload.result == "needs_human"
    assert payload.human_task is not None


def test_parse_result_payload_rejects_human_task_slug() -> None:
    payload, warnings = _parse_result_payload(
        json.dumps({
            "result": "needs_human",
            "blocker": "missing secret",
            "human_task": {
                "slug": "provide-secret",
                "title": "Provide secret",
                "body": "Add the production secret.",
                "acceptance_criteria": ["Secret is available"],
            },
        })
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

    process = SimpleNamespace(pid=123, stdin=stdin, stdout=stdout, poll=lambda: None)
    runtime._process = cast(Any, process)

    response = runtime._rpc_request("get_state")

    assert response["success"] is True
    assert json.loads(stdin.getvalue()) == {"type": "get_state"}


def test_call_with_runtime_starts_and_stops_unhealthy_pi_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PiRuntime(binary="pi")
    calls: list[tuple[object, ...]] = []

    @contextmanager
    def fake_runtime_env(**_kwargs: object):
        yield {"JRI_PI_PACKAGE": "package"}

    def fake_start(*, env: dict[str, str] | None = None, cwd: Path | None = None) -> None:
        calls.append(("start", env, cwd))

    def fake_stop() -> None:
        calls.append(("stop",))

    monkeypatch.setattr("jri.core.agents.session.runtime_env", fake_runtime_env)
    monkeypatch.setattr(runtime, "start", fake_start)
    monkeypatch.setattr(runtime, "stop", fake_stop)

    assert call_with_runtime(runtime, root=tmp_path, operation=lambda: "ok") == "ok"
    assert calls == [("start", {"JRI_PI_PACKAGE": "package"}, tmp_path), ("stop",)]


def test_session_list_sessions_delegates_to_pi_runtime(tmp_path: Path) -> None:
    def fake_list_sessions(self: PiRuntime, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
        del self, limit
        return [{"id": "ses_123", "directory": str(root)}]

    runtime = PiRuntime(binary="pi")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(PiRuntime, "list_sessions", fake_list_sessions)
    try:
        assert session_list_sessions(runtime, root=tmp_path) == [{"id": "ses_123", "directory": str(tmp_path)}]
    finally:
        monkeypatch.undo()


def test_session_list_sessions_uses_call_with_runtime_for_other_runtimes(tmp_path: Path) -> None:
    class SimpleRuntime:
        def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
            del limit
            return [{"id": "ses_456", "directory": str(root)}]

    assert session_list_sessions(cast(Any, SimpleRuntime()), root=tmp_path) == [
        {"id": "ses_456", "directory": str(tmp_path)}
    ]


def test_detect_latest_session_prefers_new_session_not_in_before(tmp_path: Path) -> None:
    sessions: list[dict[str, object]] = [
        {"id": "ses_seen", "directory": str(tmp_path)},
        {"id": "ses_new", "directory": str(tmp_path)},
    ]

    assert detect_latest_session(root=tmp_path, before={"ses_seen"}, sessions=sessions) == "ses_new"


def test_detect_latest_session_falls_back_to_previous_match(tmp_path: Path) -> None:
    sessions: list[dict[str, object]] = [{"id": "ses_seen", "directory": str(tmp_path)}]

    assert detect_latest_session(root=tmp_path, before={"ses_seen"}, sessions=sessions) == "ses_seen"


def test_detect_latest_session_returns_none_when_nothing_matches(tmp_path: Path) -> None:
    sessions: list[dict[str, object]] = [{"id": "ses_other", "directory": str(tmp_path / "elsewhere")}]

    assert detect_latest_session(root=tmp_path, before=set(), sessions=sessions) is None


def test_detect_latest_session_ignores_non_string_entries(tmp_path: Path) -> None:
    sessions: list[dict[str, object]] = [{"id": 1, "directory": 2}]

    assert detect_latest_session(root=tmp_path, before=set(), sessions=sessions) is None


def test_export_session_if_available_records_timeline_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingRuntime:
        model = None

        def list_sessions(self, *, _root: Path, _limit: int = 20) -> list[dict[str, object]]:
            return []

        def run_ralph_task(
            self,
            *,
            _root: Path,
            _prompt: str,
            _log_path: Path,
            _result_path: Path,
            _on_start: object | None = None,
            _timeout: int | None = None,
        ) -> object:
            return object()

        def export_session(self, _session_id: str, _destination: Path) -> None:
            raise JriError("boom")

        def compile_intent_graph(self, *, _root: Path, _context: dict[str, object]) -> dict[str, object]:
            return {}

    runtime = FailingRuntime()
    timeline_store = TimelineStore(tmp_path / "timeline.jsonl")
    monkeypatch.setattr(TimelineStore, "now_iso", lambda: "2025-01-01T00:00:00Z")

    assert (
        export_session_if_available(
            cast(Any, runtime),
            root=tmp_path,
            destination_dir=tmp_path / "exports",
            timeline=timeline_store,
            session_id="ses_123",
            task_slug="task-a",
        )
        is None
    )
    assert "Failed to export session ses_123: boom" in capsys.readouterr().err

    events = timeline_store.read()
    assert events == [
        TimelineEvent(
            ts="2025-01-01T00:00:00Z",
            event="export_failed",
            task="task-a",
            detail={"session_id": "ses_123", "error": "boom"},
        )
    ]


def test_export_session_if_available_returns_none_without_session_id(tmp_path: Path) -> None:
    class SimpleRuntime:
        def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
            del root, limit
            return []

    timeline_store = TimelineStore(tmp_path / "timeline.jsonl")

    assert (
        export_session_if_available(
            cast(Any, SimpleRuntime()),
            root=tmp_path,
            destination_dir=tmp_path / "exports",
            timeline=timeline_store,
            session_id=None,
        )
        is None
    )
    assert timeline_store.read() == []


def test_export_session_if_available_exports_session_successfully(tmp_path: Path) -> None:
    class ExportingRuntime:
        model = None

        def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
            del root, limit
            return []

        def run_ralph_task(self, **kwargs: object) -> object:
            del kwargs
            return object()

        def export_session(self, session_id: str, destination: Path) -> None:
            destination.write_text(f"exported:{session_id}", encoding="utf-8")

        def compile_intent_graph(self, **kwargs: object) -> dict[str, object]:
            del kwargs
            return {}

    timeline_store = TimelineStore(tmp_path / "timeline.jsonl")
    destination = export_session_if_available(
        cast(Any, ExportingRuntime()),
        root=tmp_path,
        destination_dir=tmp_path / "exports",
        timeline=timeline_store,
        session_id="ses_123",
    )

    assert destination == tmp_path / "exports" / "ses_123.json"
    assert destination is not None
    assert destination.read_text(encoding="utf-8") == "exported:ses_123"


def test_resource_manifest_validates_ids_and_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    resources.resource_manifest.cache_clear()
    try:
        for payload, expected_message in [
            ("[]", "must be an object"),
            ('{"": "extension.ts"}', "non-empty strings"),
            ('{"extensions.default": 3}', "must be a string"),
        ]:

            def fake_files(package: str, payload: str = payload) -> object:
                del package
                return _manifest_files(payload)

            monkeypatch.setattr(resources, "files", fake_files)
            with pytest.raises(ValueError, match=expected_message):
                resources.resource_manifest()
            resources.resource_manifest.cache_clear()
    finally:
        resources.resource_manifest.cache_clear()


def test_validate_manifest_path_rejects_unsafe_paths() -> None:
    with pytest.raises(ValueError, match="relative"):
        resources._validate_manifest_path("bad.absolute", "/etc/passwd")
    with pytest.raises(ValueError, match="traverse"):
        resources._validate_manifest_path("bad.parent", "../outside")
    with pytest.raises(ValueError, match="POSIX"):
        resources._validate_manifest_path("bad.separator", "extensions\\bad.ts")


def test_resource_lookup_resolves_paths() -> None:
    resources.resource_manifest.cache_clear()
    assert resources.resource_relative_path("extensions.default") == "extension.ts"

    resolved = resources.resource_path("extensions.default")

    assert resolved.is_file()
    assert resolved.name == "extension.ts"
    with pytest.raises(ValueError, match="unknown agent resource ID: missing.resource"):
        resources.resource_relative_path("missing.resource")


def test_model_payloads_include_optional_fields() -> None:
    human_task = HumanTaskPayload(
        title="Provide secret", body="Add the production secret.", acceptance_criteria=["Secret is available"]
    )
    enriched_human_task = HumanTaskPayload(
        title="Provide secret",
        body="Add the production secret.",
        acceptance_criteria=["Secret is available"],
        priority=3,
    )
    result_payload = RalphResultPayload(result="completed")
    enriched_result_payload = RalphResultPayload(
        result="needs_human",
        summary="Blocked",
        learnings=["Capture the blocker."],
        blocker="missing secret",
        human_task=enriched_human_task,
    )
    attempt = AttemptState(number=1, task_slug="task-a", branch="main", started_at=123)
    enriched_attempt = AttemptState(
        number=2,
        task_slug="task-b",
        branch="main",
        started_at=123,
        finished_at=456,
        log_path=".jri/logs/ralph/task-b.log",
        session_id="ses_123",
        result="timeout",
        result_payload=enriched_result_payload,
    )

    assert human_task.to_payload() == {
        "title": "Provide secret",
        "body": "Add the production secret.",
        "acceptance_criteria": ["Secret is available"],
    }
    assert enriched_human_task.to_payload()["priority"] == 3
    assert result_payload.to_payload() == {"result": "completed"}
    assert enriched_result_payload.to_payload() == {
        "result": "needs_human",
        "summary": "Blocked",
        "learnings": ["Capture the blocker."],
        "blocker": "missing secret",
        "human_task": enriched_human_task.to_payload(),
    }
    assert attempt.to_payload() == {"number": 1, "task_slug": "task-a", "branch": "main", "started_at": 123}
    assert enriched_attempt.to_payload() == {
        "number": 2,
        "task_slug": "task-b",
        "branch": "main",
        "started_at": 123,
        "finished_at": 456,
        "log_path": ".jri/logs/ralph/task-b.log",
        "session_id": "ses_123",
        "result": "timeout",
        "result_payload": enriched_result_payload.to_payload(),
    }


def test_state_payloads_round_trip_nested_metadata() -> None:
    state = State.from_payload({
        "started_at": 1,
        "finished_at": 2,
        "session": "ses_123",
        "branch": "main",
        "process": {"loop_pid": 11, "child_pid": 12, "log_path": ".jri/logs/ralph/task-a.log", "detached": True},
        "active_attempt": {"task_slug": "task-a", "branch": "ralph", "result": "incomplete"},
        "attempts": [
            {
                "number": 2,
                "task_slug": "task-b",
                "branch": "main",
                "started_at": 123,
                "finished_at": 456,
                "result": "timeout",
                "result_payload": {
                    "result": "needs_human",
                    "summary": "Blocked",
                    "learnings": ["Capture the blocker."],
                    "blocker": "missing secret",
                    "human_task": {
                        "title": "Provide secret",
                        "body": "Add the production secret.",
                        "acceptance_criteria": ["Secret is available"],
                        "priority": 3,
                    },
                },
            }
        ],
        "current_task": "task-a",
    })

    assert state == State(
        started_at=1,
        finished_at=2,
        session="ses_123",
        process=ProcessState(loop_pid=11, child_pid=12, log_path=".jri/logs/ralph/task-a.log", detached=True),
        branch="main",
        active_attempt=AttemptState(number=0, task_slug="task-a", branch="ralph", started_at=0, result="incompleted"),
        attempts=[
            AttemptState(
                number=2,
                task_slug="task-b",
                branch="main",
                started_at=123,
                finished_at=456,
                result="timeout",
                result_payload=RalphResultPayload.from_payload({
                    "result": "needs_human",
                    "summary": "Blocked",
                    "learnings": ["Capture the blocker."],
                    "blocker": "missing secret",
                    "human_task": {
                        "title": "Provide secret",
                        "body": "Add the production secret.",
                        "acceptance_criteria": ["Secret is available"],
                        "priority": 3,
                    },
                }),
            )
        ],
        current_task="task-a",
    )
    payload = state.to_payload()
    assert cast(dict[str, object], payload["active_attempt"])["result"] == "incompleted"


def test_state_payload_defaults_round_trip_empty_state() -> None:
    state = State()

    assert state.to_payload() == {}
    assert State.from_payload({}) == State()


def test_attempt_state_from_payload_drops_unknown_result() -> None:
    attempt = AttemptState.from_payload({
        "task_slug": "task-a",
        "branch": "main",
        "started_at": 123,
        "result": "mystery",
    })

    assert attempt.result is None


def test_jri_paths_construct_expected_paths(tmp_path: Path) -> None:
    paths = JriPaths(tmp_path)

    assert paths.jri_dir == tmp_path / ".jri"
    assert paths.tasks_dir == tmp_path / ".jri" / "tasks"
    assert paths.task_dir("doing") == tmp_path / ".jri" / "tasks" / "doing"
    assert paths.graph_dir == tmp_path / ".jri" / "graph"
    assert paths.signals_dir == tmp_path / ".jri" / "signals"
    assert paths.logs_dir == tmp_path / ".jri" / "logs"
    assert paths.ralph_logs_dir == tmp_path / ".jri" / "logs" / "ralph"
    assert paths.external_logs_dir == tmp_path / ".jri" / "logs" / "external"
    assert paths.chat_logs_dir == tmp_path / ".jri" / "logs" / "chat"
    assert paths.external_pi_dir == tmp_path / ".jri" / "logs" / "external" / "pi"
    assert paths.diffs_dir == tmp_path / ".jri" / "logs" / "diffs"
    assert paths.state_path == tmp_path / ".jri" / "state.json"
    assert paths.root_gitignore_path == tmp_path / ".gitignore"
    assert paths.gitignore_path == tmp_path / ".jri" / ".gitignore"
    assert paths.readme_path == tmp_path / "README.md"
    assert paths.stop_signal_path == tmp_path / ".jri" / "signals" / "stop"
    assert paths.recovery_log_path == tmp_path / ".jri" / "logs" / "recovery.log"
    assert paths.recovery_failures_log_path == tmp_path / ".jri" / "logs" / "recovery-failures.log"
    assert paths.timeline_path == tmp_path / ".jri" / "logs" / "timeline.jsonl"
    assert paths.metrics_path == tmp_path / ".jri" / "metrics.json"
    assert paths.learnings_path == tmp_path / ".jri" / "learnings.md"
    assert paths.attempts_dir == tmp_path / ".jri" / "attempts"
    assert paths.worktree_dir == tmp_path / ".jri" / "worktree"
    assert paths.task_path("doing", "task-a") == tmp_path / ".jri" / "tasks" / "doing" / "task-a.md"
    assert paths.diff_artifact_path("task-a") == tmp_path / ".jri" / "logs" / "diffs" / "task-a.diff"
    assert paths.attempt_history_path("task-a") == tmp_path / ".jri" / "attempts" / "task-a.yaml"
    assert paths.ralph_log_path("task-a", 0).name == "1970-01-01T00-00-00Z-task-a.log"
    assert paths.graph_node_path("product/checkout") == graph_node_path(tmp_path, "product/checkout")


def test_package_manifest_uses_resource_manifest_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resource_paths = {
        "extensions.default": "extension.ts",
        "prompts.interrogator": "interrogator/prompt.md",
        "tools.pythonRunner": "(shared)/runner.ts",
        "themes.modernYellow": "theme.json",
    }
    resolved_ids: list[str] = []

    def fake_resource_relative_path(resource_id: str) -> str:
        resolved_ids.append(resource_id)
        return resource_paths[resource_id]

    monkeypatch.setattr("jri.core.agents.session.resource_relative_path", fake_resource_relative_path)

    _write_package_manifest(tmp_path, overrides={"ralph": "test-model"})

    package = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert package["pi"] == {
        "extensions": ["./extension.ts"],
        "skills": ["./interrogator/skills", "./ralph/skills"],
        "prompts": ["./interrogator"],
        "tools": ["./(shared)"],
        "themes": ["./theme.json"],
    }
    assert resolved_ids == ["extensions.default", "prompts.interrogator", "tools.pythonRunner", "themes.modernYellow"]
    assert package["jri"]["models"] == {"ralph": "test-model"}


def test_runtime_env_copies_complete_agent_bundle() -> None:
    with runtime_env(overrides={}) as env:
        package_root = Path(env["JRI_PI_PACKAGE"])

        assert not (package_root / "__init__.py").exists()
        assert (package_root / "manifest.json").is_file()
        assert (package_root / "compiler" / "prompt.md").is_file()
        assert (package_root / "(shared)" / "runner.ts").is_file()
        assert not (package_root / "(shared)" / "__init__.py").exists()
        assert not (package_root / "interrogator" / "validator").exists()
        project_setup_skill = package_root / "ralph" / "skills" / "project-setup" / "SKILL.md"
        assert project_setup_skill.is_file()
        assert not any(path.suffix == ".py" for path in package_root.rglob("*"))
        assert not any("__pycache__" in path.parts for path in package_root.rglob("*"))


def test_pi_runtime_start_appends_ralph_prompt_and_loads_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "package"
    (package_root / "ralph" / "skills" / "hosted-projects").mkdir(parents=True)
    (package_root / "ralph" / "skills" / "project-setup").mkdir(parents=True)
    (package_root / "interrogator" / "skills" / "reverse-ralph").mkdir(parents=True)
    (package_root / "extension.ts").write_text("", encoding="utf-8")
    (package_root / "ralph" / "prompt.md").write_text("", encoding="utf-8")

    resolved_ids: list[str] = []
    resource_paths = {"extensions.default": "extension.ts", "prompts.ralph": "ralph/prompt.md"}

    def fake_resource_relative_path(resource_id: str) -> str:
        resolved_ids.append(resource_id)
        return resource_paths[resource_id]

    monkeypatch.setattr("jri.core.agents.client.resource_relative_path", fake_resource_relative_path)

    popen_calls: list[list[str]] = []
    popen_envs: list[dict[str, str]] = []

    def fake_popen(*args: object, **kwargs: object) -> object:
        popen_calls.append(cast(list[str], args[0]))
        popen_envs.append(cast(dict[str, str], kwargs["env"]))
        return SimpleNamespace(
            pid=123,
            stdin=io.StringIO(),
            stdout=io.StringIO('{"type":"response","command":"get_state","success":true}\n'),
            poll=lambda: None,
        )

    monkeypatch.setattr("jri.core.agents.client.subprocess.Popen", fake_popen)
    monkeypatch.setenv("JRI_CHAT_RUNTIME", "1")

    runtime = PiRuntime(binary="pi")
    runtime.start(env={"JRI_PI_PACKAGE": str(package_root), "JRI_CHAT_RUNTIME": "1"}, cwd=tmp_path)

    assert popen_calls == [
        [
            "pi",
            "--mode",
            "rpc",
            "--session-dir",
            str(tmp_path / ".jri" / "logs" / "external" / "pi" / "sessions"),
            "--extension",
            str(package_root / "extension.ts"),
            "--append-system-prompt",
            str(package_root / "ralph" / "prompt.md"),
            "--skill",
            str(package_root / "ralph" / "skills" / "hosted-projects"),
            "--skill",
            str(package_root / "ralph" / "skills" / "project-setup"),
        ]
    ]
    assert str(package_root / "interrogator" / "validator" / "extension.ts") not in popen_calls[0]
    assert resolved_ids == ["extensions.default", "prompts.ralph"]
    assert "JRI_CHAT_RUNTIME" not in popen_envs[0]


def test_pi_runtime_uses_fresh_rpc_process_for_each_ralph_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = PiRuntime(binary="pi")
    runtime._env = {"JRI_PI_PACKAGE": "package"}
    runtime._process = cast(Any, SimpleNamespace(pid=111, stdin=io.StringIO(), stdout=io.StringIO(), poll=lambda: None))
    result_path = tmp_path / "result.json"
    starts: list[Path | None] = []
    stops: list[int] = []

    def fake_stop() -> None:
        stops.append(1)
        runtime._process = None

    def fake_start(*, env: dict[str, str] | None = None, cwd: Path | None = None) -> None:
        assert env == {"JRI_PI_PACKAGE": "package"}
        starts.append(cwd)
        runtime._process = cast(
            Any, SimpleNamespace(pid=222, stdin=io.StringIO(), stdout=io.StringIO(), poll=lambda: None)
        )
        runtime._session_id = "ses_fresh"

    def fake_rpc_request(command: str, extra: dict[str, object] | None = None) -> dict[str, object]:
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
        root=tmp_path, prompt="do task", log_path=tmp_path / "ralph.log", result_path=result_path
    )

    assert stops == [1]
    assert starts == [tmp_path]
    assert result.session_id == "ses_fresh"
    assert result.result == "completed"


def test_launch_chat_appends_interrogator_prompt_and_loads_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "package"
    package_root.mkdir()
    (package_root / "extension.ts").write_text("", encoding="utf-8")
    (package_root / "interrogator" / "validator").mkdir(parents=True)
    (package_root / "interrogator" / "validator" / "extension.ts").write_text("", encoding="utf-8")
    (package_root / "interrogator" / "prompt.md").write_text("", encoding="utf-8")
    (package_root / "interrogator" / "skills" / "reverse-ralph").mkdir(parents=True)
    resolved_ids: list[str] = []
    resource_paths = {"extensions.default": "extension.ts", "prompts.interrogator": "interrogator/prompt.md"}

    def fake_resource_relative_path(resource_id: str) -> str:
        resolved_ids.append(resource_id)
        return resource_paths[resource_id]

    monkeypatch.setattr("jri.core.agents.client.resource_relative_path", fake_resource_relative_path)

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
            session_dir=tmp_path / ".jri" / "logs" / "chat",
        )
        == 0
    )

    assert run_calls == [
        [
            "pi",
            "--session-dir",
            str(tmp_path / ".jri" / "logs" / "chat"),
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--extension",
            str(package_root / "extension.ts"),
            "--append-system-prompt",
            str(package_root / "interrogator" / "prompt.md"),
            "--skill",
            str(package_root / "interrogator" / "skills" / "reverse-ralph"),
            "--tools",
            (
                "create-node,list-nodes,read-node,search-nodes,"
                "apply-graph-patch,"
                "update-node-metadata,move-node,compile-graph,"
                "list-tasks,read-tasks,read-readme,edit-readme,"
                "explore"
            ),
        ]
    ]
    assert str(package_root / "interrogator" / "validator" / "extension.ts") not in run_calls[0]
    assert resolved_ids == ["extensions.default", "prompts.interrogator"]
    assert run_envs[0]["JRI_CHAT_RUNTIME"] == "1"


def test_launch_chat_rejects_capability_args(tmp_path: Path) -> None:
    with pytest.raises(JriError, match="manages Pi capabilities"):
        launch_chat(root=tmp_path, session_id=None, extra_args=["--tools", "read"], binary="pi", env={})


def test_pi_runtime_lists_repo_session_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    session_dir = repo / ".jri" / "logs" / "chat"
    cwd_dir = session_dir / "--repo--"
    cwd_dir.mkdir(parents=True)
    session_file = cwd_dir / "2026-05-02T00-00-00-000Z_ses_123.jsonl"
    session_file.write_text(
        json.dumps({"type": "session", "version": 3, "id": "ses_123", "cwd": str(repo)}) + "\n", encoding="utf-8"
    )
    other_file = cwd_dir / "2026-05-02T00-00-01-000Z_ses_other.jsonl"
    other_file.write_text(
        json.dumps({"type": "session", "version": 3, "id": "ses_other", "cwd": str(tmp_path / "other")}) + "\n",
        encoding="utf-8",
    )

    sessions = PiRuntime(binary="pi").list_sessions(root=repo)

    assert sessions == [{"id": "ses_123", "directory": str(repo), "sessionFile": str(session_file)}]


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
