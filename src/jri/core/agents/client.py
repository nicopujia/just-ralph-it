import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Protocol, cast

from ..errors import JriError
from ..models import AgentRunResult, RalphResult, RalphResultPayload, Result
from ..ui import DIM, _s, trim_tool_output


def _tool_detail(tool_name: str, input_obj: object, *, cwd_hint: str = "") -> str:
    """Extract a one-line detail (file path, command, etc.) from tool input."""
    if not isinstance(input_obj, dict):
        return ""
    input_obj = cast(dict[str, object], input_obj)

    def _rel(path: str) -> str:
        if cwd_hint and path.startswith(cwd_hint):
            return path[len(cwd_hint) :] or "."
        return path

    candidates_by_tool: dict[str, tuple[str, ...]] = {
        "read": ("filePath", "path"),
        "write": ("filePath", "path"),
        "edit": ("filePath", "path"),
        "multiedit": ("filePath", "path"),
        "glob": ("pattern",),
        "grep": ("pattern",),
        "list": ("path",),
        "ls": ("path",),
        "bash": ("description", "command"),
        "webfetch": ("url",),
        "task": ("description",),
        "todowrite": ("",),
    }
    keys = candidates_by_tool.get(tool_name, ("filePath", "path", "description"))
    for key in keys:
        if not key:
            continue
        value = input_obj.get(key)
        if isinstance(value, str) and value:
            if key in ("filePath", "path"):
                value = _rel(value)
            if len(value) > 80:
                value = value[:77] + "..."
            return value
    return ""


def _unwrap_event(event: dict[str, object]) -> dict[str, object]:
    payload = event.get("payload")
    if isinstance(payload, dict):
        return cast(dict[str, object], payload)
    return event


def render_saved_event(
    event: dict[str, object], *, seen_tool_calls: set[str], cwd_hint: str = ""
) -> tuple[str, bool]:
    """Return rendered output for a persisted Pi event."""
    event = _unwrap_event(event)
    etype = event.get("type")
    if etype == "message.part.delta":
        properties = event.get("properties")
        if not isinstance(properties, dict):
            return "", False
        properties = cast(dict[str, object], properties)
        if properties.get("field") != "text":
            return "", False
        delta = properties.get("delta")
        return (delta, False) if isinstance(delta, str) else ("", False)
    if etype == "message.part.updated":
        properties = event.get("properties")
        if not isinstance(properties, dict):
            return "", False
        properties = cast(dict[str, object], properties)
        part = properties.get("part")
        if not isinstance(part, dict):
            return "", False
        part = cast(dict[str, object], part)
        if part.get("type") != "tool":
            return "", False
        state = part.get("state")
        if not isinstance(state, dict):
            return "", False
        state = cast(dict[str, object], state)
        if state.get("status") != "running":
            return "", False
        call_id = part.get("callID") or part.get("id")
        if isinstance(call_id, str):
            if call_id in seen_tool_calls:
                return "", False
            seen_tool_calls.add(call_id)
        tool_name = cast(str, part.get("tool") or state.get("tool") or "tool")
        detail = _tool_detail(tool_name, state.get("input"), cwd_hint=cwd_hint)
        label = f"⚙ {tool_name}"
        if detail:
            label = f"{label} {detail}"
        return _s(label, DIM) + "\n", True
    if etype == "message_update":
        delta = event.get("delta") or event.get("text")
        return (delta, False) if isinstance(delta, str) else ("", False)
    if etype == "message_start":
        return "", False
    if etype == "message_end":
        return "\n", False
    if etype not in {"tool_execution_start", "tool_execution_update"}:
        return "", False
    call_id = event.get("toolCallId") or event.get("id")
    if isinstance(call_id, str):
        if call_id in seen_tool_calls:
            return "", False
        seen_tool_calls.add(call_id)
    tool_name = cast(str, event.get("toolName") or event.get("name") or "tool")
    detail = _tool_detail(tool_name, event.get("input"), cwd_hint=cwd_hint)
    label = f"⚙ {tool_name}"
    if detail:
        label = f"{label} {detail}"
    return _s(label, DIM) + "\n", True


class SavedLogRenderer:
    """Incrementally reconstruct terminal output from saved Pi RPC logs."""

    def __init__(self, *, cwd_hint: str = "") -> None:
        self._cwd_hint = cwd_hint
        self._seen_tool_calls: set[str] = set()
        self._buffer = ""
        self._last_terminal_char = "\n"
        self.active_task_detail: str | None = None
        self._active_task_call_id: str | None = None

    def render_chunk(self, text: str, *, final: bool = False) -> str:
        if text:
            self._buffer += text
        rendered: list[str] = []
        while True:
            newline_index = self._buffer.find("\n")
            if newline_index < 0:
                break
            raw_line = self._buffer[: newline_index + 1]
            self._buffer = self._buffer[newline_index + 1 :]
            self._append_line(rendered, raw_line)
        if final and self._buffer:
            self._append_line(rendered, self._buffer)
            self._buffer = ""
        return "".join(rendered)

    def render_event(self, event: dict[str, object]) -> tuple[str, bool]:
        self._track_task_state(event)
        return render_saved_event(
            event,
            seen_tool_calls=self._seen_tool_calls,
            cwd_hint=self._cwd_hint,
        )

    def _append_line(self, rendered: list[str], raw_line: str) -> None:
        line = raw_line.rstrip("\n")
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            rendered.append(raw_line)
            if raw_line:
                self._last_terminal_char = raw_line[-1]
            return
        if not isinstance(event, dict):
            return
        text_to_print, newline_before = self.render_event(event)
        if not text_to_print:
            return
        if newline_before and self._last_terminal_char != "\n":
            rendered.append("\n")
        rendered.append(text_to_print)
        self._last_terminal_char = text_to_print[-1]

    def _track_task_state(self, event: dict[str, object]) -> None:
        event = _unwrap_event(event)
        if event.get("type") == "message.part.updated":
            properties = event.get("properties")
            if not isinstance(properties, dict):
                return
            properties = cast(dict[str, object], properties)
            part = properties.get("part")
            if not isinstance(part, dict):
                return
            part = cast(dict[str, object], part)
            if part.get("type") != "tool" or part.get("tool") != "task":
                return
            state = part.get("state")
            if not isinstance(state, dict):
                return
            state = cast(dict[str, object], state)
            if state.get("status") != "running":
                return
            call_id = part.get("callID") or part.get("id")
            input_obj = state.get("input")
        else:
            if event.get("type") != "tool_execution_start":
                return
            tool_name = event.get("toolName") or event.get("name")
            if tool_name != "task":
                return
            call_id = event.get("toolCallId") or event.get("id")
            input_obj = event.get("input")
        detail = _tool_detail("task", input_obj, cwd_hint=self._cwd_hint)
        self.active_task_detail = detail or "task"
        self._active_task_call_id = call_id if isinstance(call_id, str) else None


def render_saved_log(text: str, *, cwd_hint: str = "") -> str:
    """Reconstruct terminal output from a saved per-task Pi log."""
    return SavedLogRenderer(cwd_hint=cwd_hint).render_chunk(text, final=True)


def _parse_event_line(line: str) -> tuple[dict[str, object] | None, str | None, bool]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None, line, False
    if not isinstance(payload, dict):
        return None, None, False
    event_type = payload.get("type")
    if event_type == "message_update":
        text = payload.get("delta") or payload.get("text")
        return payload, text if isinstance(text, str) else None, False
    if event_type in {"tool_execution_update", "tool_execution_end"}:
        output = payload.get("output") or payload.get("result")
        if isinstance(output, str) and output:
            trimmed = trim_tool_output(output)
            return payload, trimmed if trimmed is not None else output, True
    return payload, None, False


def _normalize_result(raw: str) -> RalphResult | None:
    if raw == "completed":
        return "completed"
    if raw == "needs_human":
        return "needs_human"
    if raw in {"incomplete", "incompleted"}:
        return "incompleted"
    return None


def _missing_result_payload(*, context: str) -> tuple[Result, list[str]]:
    warning = f"missing result payload for {context}; treating run as failed"
    print(warning, file=sys.stderr)
    return "failed", [warning]


def _parse_result_payload(text: str) -> tuple[RalphResultPayload | None, list[str]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        warning = f"invalid result payload; treating run as failed: {exc}"
        print(warning, file=sys.stderr)
        return None, [warning]
    if not isinstance(payload, dict):
        warning = "invalid result payload; treating run as failed: expected object"
        print(warning, file=sys.stderr)
        return None, [warning]
    result = payload.get("result")
    normalized = _normalize_result(result) if isinstance(result, str) else None
    if normalized is None:
        warning = (
            "invalid result payload; treating run as failed: "
            "missing or unknown `result`"
        )
        print(warning, file=sys.stderr)
        return None, [warning]
    parsed = RalphResultPayload.from_payload(cast(dict[str, object], payload))
    if parsed.result == "incompleted" and not parsed.learnings:
        warning = (
            "invalid result payload; treating run as failed: "
            "`incompleted` requires non-empty `learnings`"
        )
        print(warning, file=sys.stderr)
        return None, [warning]
    if parsed.result == "needs_human":
        if not parsed.blocker or parsed.human_task is None:
            warning = (
                "invalid result payload; treating run as failed: "
                "`needs_human` requires `blocker` and `human_task`"
            )
            print(warning, file=sys.stderr)
            return None, [warning]
        validation_error = _validate_human_task_payload(payload)
        if validation_error is not None:
            warning = (
                f"invalid result payload; treating run as failed: {validation_error}"
            )
            print(warning, file=sys.stderr)
            return None, [warning]
    return parsed, []


def _validate_human_task_payload(payload: dict[str, object]) -> str | None:
    human_task = payload.get("human_task")
    if not isinstance(human_task, dict):
        return "`human_task` must be an object"
    human_task = cast(dict[str, object], human_task)

    if "slug" in human_task:
        return "`human_task.slug` is not supported; JRI derives the Human task slug"

    title = human_task.get("title")
    if not isinstance(title, str) or not title.strip():
        return "`human_task.title` must be a non-empty string"

    body = human_task.get("body")
    if not isinstance(body, str) or not body.strip():
        return "`human_task.body` must be a non-empty string"

    acceptance = human_task.get("acceptance_criteria")
    if not isinstance(acceptance, list) or not acceptance:
        return "`human_task.acceptance_criteria` must be a non-empty string list"
    if not all(isinstance(item, str) and item.strip() for item in acceptance):
        return "`human_task.acceptance_criteria` must be a non-empty string list"

    priority = human_task.get("priority")
    if priority is not None and (
        not isinstance(priority, int)
        or isinstance(priority, bool)
        or not 0 <= priority <= 4
    ):
        return "`human_task.priority` must be an integer between 0 and 4"

    return None


class AgentRuntime(Protocol):
    model: str | None

    def list_sessions(
        self, *, root: Path, limit: int = 20
    ) -> list[dict[str, object]]: ...

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: Callable[[int], None] | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult: ...

    def export_session(self, session_id: str, destination: Path) -> None: ...


def launch_chat(
    *,
    root: Path,
    session_id: str | None,
    extra_args: list[str],
    binary: str = "pi",
    env: dict[str, str] | None = None,
    session_dir: Path | None = None,
) -> int:
    command = [binary]
    if session_dir is not None:
        session_dir.mkdir(parents=True, exist_ok=True)
        command.extend(["--session-dir", str(session_dir)])
    if session_id:
        command.extend(["--session", session_id])
    if env and (package_root := env.get("JRI_PI_PACKAGE")):
        extension_path = Path(package_root) / "extensions" / "jri.ts"
        prompt_path = Path(package_root) / "prompts" / "interrogator.md"
        command.extend(["--extension", str(extension_path)])
        command.extend(["--append-system-prompt", str(prompt_path)])
    command.extend(extra_args)
    try:
        merged_env = os.environ.copy()
        merged_env["JRI_PYTHON"] = sys.executable
        if env:
            merged_env.update(env)
        merged_env["JRI_CHAT_RUNTIME"] = "1"
        result = subprocess.run(command, cwd=root, env=merged_env, check=False)
        return result.returncode
    except FileNotFoundError as err:
        raise JriError(f"could not find `{binary}` — is Pi installed?") from err


_RUN_STALL_TIMEOUT = 300.0
_MISSING_RESULT_FOLLOW_UP_PROMPT = (
    "Your last response ended without the required result payload. "
    "Final action only: call `ralph-result` exactly once with the correct payload, "
    "then stop."
)


def _repo_pi_chat_session_dir(root: Path) -> Path:
    return root / ".jri" / "logs" / "chat"


def _repo_pi_ralph_session_dir(root: Path) -> Path:
    return root / ".jri" / "logs" / "external" / "pi" / "sessions"


def _pi_session_dirs(root: Path) -> list[Path]:
    return [_repo_pi_chat_session_dir(root)]


def _list_pi_session_files(root: Path, *, limit: int) -> list[dict[str, object]]:
    root_resolved = root.resolve()
    sessions: list[tuple[float, dict[str, object]]] = []
    for session_dir in _pi_session_dirs(root):
        if not session_dir.exists():
            continue
        for session_file in session_dir.rglob("*.jsonl"):
            session = _read_pi_session_header(session_file)
            if session is None:
                continue
            directory = session.get("directory")
            if not isinstance(directory, str):
                continue
            try:
                if Path(directory).resolve() != root_resolved:
                    continue
            except OSError:
                continue
            sessions.append((session_file.stat().st_mtime, session))
    sessions.sort(key=lambda item: item[0], reverse=True)
    return [session for _, session in sessions[:limit]]


def _read_pi_session_header(session_file: Path) -> dict[str, object] | None:
    try:
        first_line = session_file.read_text(encoding="utf-8").splitlines()[0]
        payload = json.loads(first_line)
    except (OSError, IndexError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("id")
    directory = payload.get("cwd")
    if not isinstance(session_id, str) or not isinstance(directory, str):
        return None
    return {
        "id": session_id,
        "directory": directory,
        "sessionFile": str(session_file),
    }


class PiRuntime:
    """Drives Ralph sessions through `pi --mode rpc`."""

    def __init__(
        self,
        *,
        binary: str = "pi",
        port: int | None = None,
        model: str | None = None,
    ) -> None:
        del port
        self.binary = binary
        self.model = model
        self._process: subprocess.Popen[str] | None = None
        self._session_id: str | None = None
        self._session_file: Path | None = None
        self._listed_session_files: dict[str, Path] = {}
        self._cwd_hint = ""
        self._env: dict[str, str] = {}
        self._cwd: Path | None = None

    def start(
        self,
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self._session_id = None
        self._session_file = None
        merged_env = os.environ.copy()
        merged_env.pop("JRI_CHAT_RUNTIME", None)
        if env:
            merged_env.update(env)
        merged_env.pop("JRI_CHAT_RUNTIME", None)
        self._env = dict(env or {})
        self._cwd = cwd
        command = [self.binary, "--mode", "rpc"]
        if self.model:
            command.extend(["--model", self.model])
        if cwd is not None:
            session_dir = _repo_pi_ralph_session_dir(cwd)
            session_dir.mkdir(parents=True, exist_ok=True)
            command.extend(["--session-dir", str(session_dir)])
        if package_root := self._env.get("JRI_PI_PACKAGE"):
            package_path = Path(package_root)
            extension_path = package_path / "extensions" / "jri.ts"
            prompt_path = package_path / "prompts" / "ralph.md"
            command.extend(["--extension", str(extension_path)])
            command.extend(["--append-system-prompt", str(prompt_path)])
            for skill_path in sorted((package_path / "skills").iterdir()):
                if skill_path.is_dir():
                    command.extend(["--skill", str(skill_path)])
        try:
            self._process = subprocess.Popen(
                command,
                cwd=str(cwd) if cwd is not None else None,
                env=merged_env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except FileNotFoundError as err:
            message = f"could not find `{self.binary}` — is Pi installed?"
            raise JriError(message) from err
        if self._process.stdin is None or self._process.stdout is None:
            self.stop()
            raise JriError("failed to start pi rpc process")
        state = self._rpc_request("get_state")
        data = state.get("data")
        if isinstance(data, dict):
            data = cast(dict[str, object], data)
            session_id = data.get("sessionId")
            session_file = data.get("sessionFile")
            self._session_id = session_id if isinstance(session_id, str) else None
            self._session_file = (
                Path(session_file) if isinstance(session_file, str) else None
            )

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        if process.poll() is not None:
            return
        try:
            try:
                pgid = os.getpgid(process.pid)
            except (ProcessLookupError, PermissionError, OSError):
                pgid = None
            if pgid is not None:
                os.killpg(pgid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if pgid is not None:
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        except Exception:
            pass

    def is_healthy(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
        sessions = _list_pi_session_files(root=root, limit=limit)
        self._listed_session_files.update(
            {
                cast(str, session["id"]): Path(cast(str, session["sessionFile"]))
                for session in sessions
                if isinstance(session.get("id"), str)
                and isinstance(session.get("sessionFile"), str)
            }
        )
        if self.is_healthy():
            state = self._rpc_request("get_state")
            data = state.get("data")
            if isinstance(data, dict):
                data = cast(dict[str, object], data)
                session_id = data.get("sessionId")
                session_file = data.get("sessionFile")
                if isinstance(session_id, str):
                    sessions.insert(
                        0,
                        {
                            "id": session_id,
                            "directory": str(root.resolve()),
                            "sessionFile": session_file,
                        },
                    )
                    if isinstance(session_file, str):
                        self._listed_session_files[session_id] = Path(session_file)
        return sessions

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        session_file = self._listed_session_files.get(session_id)
        if session_file is None and session_id == self._session_id:
            session_file = self._session_file
        if session_file is None:
            raise JriError(f"unknown pi session '{session_id}'")
        if not session_file.exists():
            raise JriError(f"pi session file is unavailable for '{session_id}'")
        shutil.copyfile(session_file, destination)

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: Callable[[int], None] | None = None,
        timeout: int | None = None,
    ) -> AgentRunResult:
        if self.is_healthy():
            self.stop()
        self.start(env=self._env, cwd=root)
        if self._process is None or self._process.stdout is None:
            raise JriError("pi rpc process is not running")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        if result_path.exists():
            result_path.unlink()
        self._cwd_hint = str(root.resolve()).rstrip("/") + "/"
        if on_start is not None:
            on_start(self._process.pid)

        response = self._rpc_request(
            "prompt",
            {
                "message": _ralph_prompt(prompt),
            },
        )
        if response.get("success") is not True:
            raise JriError(f"failed to start ralph prompt: {response}")

        timed_out = False
        stalled = False
        deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
        last_non_heartbeat_at = time.monotonic()
        renderer = SavedLogRenderer(cwd_hint=self._cwd_hint)
        last_terminal_char = "\n"
        sent_missing_result_follow_up = False

        with log_path.open("a", encoding="utf-8") as log_file:
            while True:
                if deadline is not None and time.monotonic() > deadline:
                    timed_out = True
                    break
                if time.monotonic() - last_non_heartbeat_at > _RUN_STALL_TIMEOUT:
                    stalled = True
                    break
                event = self._read_rpc_line(timeout=0.5)
                if event is None:
                    continue
                log_file.write(json.dumps(event) + "\n")
                log_file.flush()
                if event.get("type") not in {"heartbeat", "server.heartbeat"}:
                    last_non_heartbeat_at = time.monotonic()
                text_to_print, newline_before = renderer.render_event(event)
                if text_to_print:
                    if newline_before and last_terminal_char != "\n":
                        sys.stdout.write("\n")
                    sys.stdout.write(text_to_print)
                    sys.stdout.flush()
                    last_terminal_char = text_to_print[-1]
                if event.get("type") == "agent_end":
                    if not result_path.exists() and not sent_missing_result_follow_up:
                        follow_up = self._rpc_request(
                            "prompt",
                            {"message": _MISSING_RESULT_FOLLOW_UP_PROMPT},
                        )
                        if follow_up.get("success") is not True:
                            break
                        sent_missing_result_follow_up = True
                        continue
                    break

        result: Result
        payload: RalphResultPayload | None = None
        warnings: list[str] = []
        if timed_out:
            result = "timeout"
            msg = f"pi prompt killed after {timeout}s timeout"
            print(msg, file=sys.stderr)
            warnings.append(msg)
            self.stop()
        elif stalled:
            result = "failed"
            msg = (
                "pi prompt stalled after "
                f"{int(_RUN_STALL_TIMEOUT)}s without non-heartbeat events"
            )
            print(msg, file=sys.stderr)
            warnings.append(msg)
            self.stop()
        elif result_path.exists():
            payload, warnings = _parse_result_payload(
                result_path.read_text(encoding="utf-8")
            )
            result = payload.result if payload is not None else "failed"
        else:
            result, warnings = _missing_result_payload(context="Ralph run")

        return AgentRunResult(
            returncode=0 if not timed_out else -1,
            session_id=self._session_id,
            result=result,
            payload=payload,
            warnings=warnings,
        )

    def _rpc_request(
        self, command: str, extra: dict[str, object] | None = None
    ) -> dict[str, object]:
        request: dict[str, object] = {"type": command}
        if extra:
            request.update(extra)
        self._write_rpc(request)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            event = self._read_rpc_line(timeout=0.5)
            if event is None:
                continue
            if event.get("type") == "response" and event.get("command") == command:
                return event
        raise JriError(f"pi rpc command '{command}' timed out")

    def _write_rpc(self, payload: dict[str, object]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise JriError("pi rpc process is not running")
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()

    def _read_rpc_line(self, *, timeout: float) -> dict[str, object] | None:
        process = self._process
        if process is None or process.stdout is None:
            raise JriError("pi rpc process is not running")
        line = _readline_with_timeout(process.stdout, timeout=timeout)
        if line is None:
            if process.poll() is not None:
                raise JriError("pi rpc process exited unexpectedly")
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return {"type": "raw", "text": line.rstrip("\n")}
        return payload if isinstance(payload, dict) else None


def _readline_with_timeout(stream: IO[str], *, timeout: float) -> str | None:
    if os.name == "posix":
        import select

        try:
            ready, _, _ = select.select([stream], [], [], timeout)
            if not ready:
                return None
            return stream.readline()
        except (OSError, io.UnsupportedOperation, AttributeError):
            return stream.readline()
    return stream.readline()


def _ralph_prompt(prompt: str) -> str:
    return "/ralph " + prompt
