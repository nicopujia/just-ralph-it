import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from ..errors import JriError
from ..models import OpenCodeRunResult, RalphResult, RalphResultPayload, Result
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
        "read": ("filePath",),
        "write": ("filePath",),
        "edit": ("filePath",),
        "multiedit": ("filePath",),
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
    """Return rendered output for a persisted OpenCode event."""
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
        if isinstance(delta, str) and delta:
            return delta, False
        return "", False
    if etype != "message.part.updated":
        return "", False
    properties = event.get("properties")
    if not isinstance(properties, dict):
        return "", False
    properties = cast(dict[str, object], properties)
    part = properties.get("part")
    if not isinstance(part, dict):
        return "", False
    part = cast(dict[str, object], part)
    part_type = part.get("type")
    if part_type == "reasoning":
        return "", False
    if part_type == "step-finish":
        return "\n", False
    if part_type == "text":
        return "", False
    if part_type != "tool":
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


class SavedLogRenderer:
    """Incrementally reconstruct terminal output from saved OpenCode logs."""

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
        if event.get("type") != "message.part.updated":
            return
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
        status = state.get("status")
        call_id = part.get("callID") or part.get("id")
        if status == "running":
            detail = _tool_detail("task", state.get("input"), cwd_hint=self._cwd_hint)
            self.active_task_detail = detail or "task"
            self._active_task_call_id = call_id if isinstance(call_id, str) else None
            return
        if self._active_task_call_id is None or call_id == self._active_task_call_id:
            self.active_task_detail = None
            self._active_task_call_id = None


def render_saved_log(text: str, *, cwd_hint: str = "") -> str:
    """Reconstruct terminal output from a saved per-task OpenCode log."""
    return SavedLogRenderer(cwd_hint=cwd_hint).render_chunk(text, final=True)


def _normalize_result(raw: str) -> RalphResult | None:
    if raw == "completed":
        return "completed"
    if raw == "needs_human":
        return "needs_human"
    if raw in {"incomplete", "incompleted"}:
        return "incompleted"
    return None


def _dict_value(payload: dict[str, object], key: str) -> dict[str, object] | None:
    value = payload.get(key)
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _is_thinking_event(payload: dict[str, object]) -> bool:
    if payload.get("type") != "text":
        return False
    part = _dict_value(payload, "part")
    if part is None:
        return False
    return part.get("type") == "thinking"


def _text_event_text(payload: dict[str, object]) -> str | None:
    if payload.get("type") != "text":
        return None
    part = _dict_value(payload, "part")
    if part is None:
        return None
    text = part.get("text")
    return text if isinstance(text, str) else None


def _tool_use_text(payload: dict[str, object]) -> str | None:
    if payload.get("type") != "tool_use":
        return None
    part = _dict_value(payload, "part")
    if part is None:
        return None
    state = _dict_value(part, "state")
    if state is None:
        return None
    error = state.get("error")
    if isinstance(error, str) and error:
        return error
    output = state.get("output")
    return output if isinstance(output, str) and output else None


def _parse_event_line(line: str) -> tuple[dict[str, object] | None, str | None, bool]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None, line, False

    if not isinstance(payload, dict):
        return None, None, False

    text = _text_event_text(payload)
    if text is not None:
        return payload, text, False

    tool_text = _tool_use_text(payload)
    if tool_text is not None:
        trimmed = trim_tool_output(tool_text)
        return payload, trimmed if trimmed is not None else tool_text, True

    return payload, None, False


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


class OpenCodeProgrammatic(Protocol):
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
    ) -> OpenCodeRunResult: ...

    def export_session(self, session_id: str, destination: Path) -> None: ...


def launch_chat(
    *,
    root: Path,
    session_id: str | None,
    extra_args: list[str],
    binary: str = "opencode",
    env: dict[str, str] | None = None,
) -> int:
    command = [binary, str(root), "--agent", "interrogator"]
    if session_id:
        command.extend(["--session", session_id])
    command.extend(extra_args)
    try:
        merged_env = os.environ.copy()
        merged_env["JRI_PYTHON"] = sys.executable
        if env:
            merged_env.update(env)
        return subprocess.run(command, cwd=root, env=merged_env, check=False).returncode
    except FileNotFoundError as err:
        raise JriError(f"could not find `{binary}` — is OpenCode installed?") from err


# ---------------------------------------------------------------------------
# HTTP server-based client (opencode serve)
# ---------------------------------------------------------------------------


_SERVER_HEALTH_TIMEOUT = 30.0
_SERVER_HEALTH_INTERVAL = 0.25
_RUN_STALL_TIMEOUT = 300.0
_MISSING_RESULT_FOLLOW_UP_PROMPT = (
    "Your last response ended without the required result payload. "
    "Final action only: call `ralph-result` exactly once with the correct payload, "
    "then stop."
)


def _pick_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return cast(int, sock.getsockname()[1])


def _http_request(
    method: str,
    url: str,
    *,
    body: object | None = None,
    timeout: float = 10.0,
) -> tuple[int, bytes]:
    data: bytes | None = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read() if err.fp is not None else b""


class OpenCodeServer:
    """Manages an opencode serve process and drives Ralph sessions via HTTP."""

    def __init__(
        self,
        *,
        binary: str = "opencode",
        port: int | None = None,
        model: str | None = None,
    ) -> None:
        self.binary = binary
        self._configured_port = port
        self.port: int | None = port
        self.model = model
        self._process: subprocess.Popen[bytes] | None = None
        self._base_url = (
            f"http://127.0.0.1:{port}" if port is not None else "http://127.0.0.1"
        )

    # -- lifecycle ---------------------------------------------------------

    def start(
        self,
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        port = (
            self._configured_port
            if self._configured_port is not None
            else _pick_free_local_port()
        )
        self.port = port
        self._base_url = f"http://127.0.0.1:{port}"
        try:
            self._process = subprocess.Popen(
                [self.binary, "serve", "--port", str(port)],
                cwd=str(cwd) if cwd is not None else None,
                env=merged_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.monotonic() + _SERVER_HEALTH_TIMEOUT
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    self._process = None
                    raise JriError("opencode serve exited before becoming healthy")
                if self.is_healthy():
                    return
                time.sleep(_SERVER_HEALTH_INTERVAL)
            timeout_seconds = _SERVER_HEALTH_TIMEOUT
            raise JriError(
                f"opencode serve did not become healthy within {timeout_seconds}s"
            )
        except FileNotFoundError as err:
            raise JriError(
                f"could not find `{self.binary}` — is OpenCode installed?"
            ) from err
        except BaseException:
            self.stop()
            raise

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
        try:
            status, _ = _http_request(
                "GET", f"{self._base_url}/global/health", timeout=2.0
            )
        except (urllib.error.URLError, ConnectionError, OSError):
            return False
        return status == 200

    # -- model formatting --------------------------------------------------

    def _model_payload(self) -> dict[str, str] | None:
        if not self.model:
            return None
        if "/" in self.model:
            provider_id, model_id = self.model.split("/", 1)
            return {"providerID": provider_id, "modelID": model_id}
        return {"modelID": self.model}

    # -- session APIs ------------------------------------------------------

    def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
        try:
            status, body = _http_request(
                "GET",
                f"{self._base_url}/session?limit={limit}",
                timeout=10.0,
            )
        except Exception as err:  # noqa: BLE001
            raise JriError(f"failed to list opencode sessions: {err}") from err
        if status >= 400:
            raise JriError(
                f"failed to list opencode sessions (HTTP {status}): "
                f"{body.decode('utf-8', errors='replace')}"
            )
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as err:
            raise JriError(f"invalid session list response: {err}") from err
        if not isinstance(payload, list):
            raise JriError("invalid session list response: expected array")
        sessions = [item for item in payload if isinstance(item, dict)]
        return [
            session for session in sessions if self._session_matches_root(session, root)
        ]

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            session_status, session_body = _http_request(
                "GET",
                f"{self._base_url}/session/{session_id}",
                timeout=10.0,
            )
            messages_status, messages_body = _http_request(
                "GET",
                f"{self._base_url}/session/{session_id}/message?limit=1000",
                timeout=15.0,
            )
        except Exception as err:  # noqa: BLE001
            raise JriError(f"failed to export session {session_id}: {err}") from err
        if session_status >= 400:
            raise JriError(
                f"failed to export session {session_id} (HTTP {session_status}): "
                f"{session_body.decode('utf-8', errors='replace')}"
            )
        if messages_status >= 400:
            raise JriError(
                f"failed to export session {session_id} messages "
                f"(HTTP {messages_status}): "
                f"{messages_body.decode('utf-8', errors='replace')}"
            )
        try:
            session = json.loads(session_body)
        except json.JSONDecodeError as err:
            raise JriError(f"invalid session export response: {err}") from err
        try:
            messages = json.loads(messages_body)
        except json.JSONDecodeError as err:
            raise JriError(f"invalid session message export response: {err}") from err
        destination.write_text(
            json.dumps({"session": session, "messages": messages}, indent=2) + "\n",
            encoding="utf-8",
        )

    # -- ralph task --------------------------------------------------------

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        result_path: Path,
        on_start: Callable[[int], None] | None = None,
        timeout: int | None = None,
    ) -> OpenCodeRunResult:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        if result_path.exists():
            result_path.unlink()
        # Used by _tool_detail to display paths relative to the worktree.
        self._cwd_hint = str(root).rstrip("/") + "/"

        if on_start is not None and self._process is not None:
            on_start(self._process.pid)

        # 1. Create session
        try:
            status, body = _http_request(
                "POST",
                f"{self._base_url}/session?directory={urllib.parse.quote(str(root))}",
                body={},
                timeout=15.0,
            )
        except Exception as err:  # noqa: BLE001
            raise JriError(f"failed to create opencode session: {err}") from err
        if status >= 400:
            raise JriError(
                f"failed to create opencode session (HTTP {status}): "
                f"{body.decode('utf-8', errors='replace')}"
            )
        try:
            session = json.loads(body)
        except json.JSONDecodeError as err:
            raise JriError(f"invalid session response: {err}") from err
        session_id = session.get("id") if isinstance(session, dict) else None
        if not isinstance(session_id, str):
            raise JriError("opencode session response missing id")
        if isinstance(session, dict) and not self._session_matches_root(session, root):
            self._delete_session(session_id)
            raise JriError(
                "opencode session was created for a different root than requested"
            )

        # 2. Open SSE stream and process events
        events: queue.Queue[dict[str, object] | None] = queue.Queue()
        stop_event = threading.Event()

        def _sse_reader() -> None:
            try:
                req = urllib.request.Request(
                    f"{self._base_url}/global/event",
                    headers={"Accept": "text/event-stream"},
                )
                with urllib.request.urlopen(req, timeout=None) as resp:
                    data_buf: list[str] = []
                    for raw in resp:
                        if stop_event.is_set():
                            break
                        line = raw.decode("utf-8", errors="replace").rstrip("\n")
                        if line == "":
                            if data_buf:
                                payload = "".join(data_buf)
                                data_buf = []
                                try:
                                    obj = json.loads(payload)
                                except json.JSONDecodeError:
                                    continue
                                if isinstance(obj, dict):
                                    events.put(obj)
                            continue
                        if line.startswith("data:"):
                            data_buf.append(line[5:].lstrip())
            except Exception:
                pass
            finally:
                events.put(None)

        sse_thread = threading.Thread(target=_sse_reader, daemon=True)
        sse_thread.start()

        timed_out = False
        stalled = False
        deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
        renderer = SavedLogRenderer(cwd_hint=getattr(self, "_cwd_hint", ""))
        saw_active_status = False
        last_non_heartbeat_at = time.monotonic()
        # 3. Send prompt
        self._start_ralph_prompt(
            session_id,
            prompt,
            on_error=lambda: self._delete_session(session_id),
            error_context="start ralph prompt",
        )

        # 4. Drive event loop
        last_terminal_char = "\n"
        sent_missing_result_follow_up = False
        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                while True:
                    if deadline is not None and time.monotonic() > deadline:
                        timed_out = True
                        break
                    if time.monotonic() - last_non_heartbeat_at > _RUN_STALL_TIMEOUT:
                        stalled = True
                        break
                    poll_timeout = 0.5
                    if deadline is not None:
                        poll_timeout = min(
                            poll_timeout, max(0.05, deadline - time.monotonic())
                        )
                    try:
                        event = events.get(timeout=poll_timeout)
                    except queue.Empty:
                        continue
                    if event is None:
                        break
                    log_file.write(json.dumps(event) + "\n")
                    log_file.flush()

                    if self._unwrap(event).get("type") != "server.heartbeat":
                        last_non_heartbeat_at = time.monotonic()

                    self._handle_permission(event)
                    text_to_print, newline_before = self._render_event(
                        event, session_id, renderer
                    )
                    if text_to_print:
                        if newline_before and last_terminal_char != "\n":
                            sys.stdout.write("\n")
                        sys.stdout.write(text_to_print)
                        sys.stdout.flush()
                        last_terminal_char = text_to_print[-1]

                    status_type = self._session_status_type(event, session_id)
                    if status_type in {"running", "busy"}:
                        saw_active_status = True
                    if status_type == "idle" and saw_active_status:
                        if (
                            not result_path.exists()
                            and not sent_missing_result_follow_up
                        ):
                            self._start_ralph_prompt(
                                session_id,
                                _MISSING_RESULT_FOLLOW_UP_PROMPT,
                                on_error=stop_event.set,
                                error_context="start ralph result follow-up",
                            )
                            sent_missing_result_follow_up = True
                            saw_active_status = False
                            continue
                        break
        finally:
            stop_event.set()

        # 5. Read result
        result: Result
        payload: RalphResultPayload | None = None
        warnings: list[str] = []
        if timed_out:
            result = "timeout"
            msg = f"opencode prompt killed after {timeout}s timeout"
            print(msg, file=sys.stderr)
            warnings.append(msg)
        elif stalled:
            result = "failed"
            msg = (
                "opencode prompt stalled after "
                f"{int(_RUN_STALL_TIMEOUT)}s without non-heartbeat events"
            )
            print(msg, file=sys.stderr)
            warnings.append(msg)
            self._delete_session(session_id)
        elif result_path.exists():
            payload, warnings = _parse_result_payload(
                result_path.read_text(encoding="utf-8")
            )
            result = payload.result if payload is not None else "failed"
        else:
            result, warnings = _missing_result_payload(context="Ralph run")

        return OpenCodeRunResult(
            returncode=0 if not timed_out else -1,
            session_id=session_id,
            result=result,
            payload=payload,
            warnings=warnings,
        )

    # -- helpers -----------------------------------------------------------

    def _delete_session(self, session_id: str) -> None:
        try:
            _http_request(
                "DELETE",
                f"{self._base_url}/session/{session_id}",
                timeout=5.0,
            )
        except Exception:
            pass

    def _start_ralph_prompt(
        self,
        session_id: str,
        prompt: str,
        *,
        on_error: Callable[[], None],
        error_context: str,
    ) -> None:
        prompt_body: dict[str, object] = {
            "agent": "ralph",
            "parts": [{"type": "text", "text": prompt}],
        }
        model_payload = self._model_payload()
        if model_payload is not None:
            prompt_body["model"] = model_payload
        try:
            p_status, p_body = _http_request(
                "POST",
                f"{self._base_url}/session/{session_id}/prompt_async",
                body=prompt_body,
                timeout=30.0,
            )
        except Exception as err:  # noqa: BLE001
            on_error()
            raise JriError(f"failed to {error_context}: {err}") from err
        if p_status >= 400:
            on_error()
            raise JriError(
                f"failed to {error_context} (HTTP {p_status}): "
                f"{p_body.decode('utf-8', errors='replace')}"
            )

    def _handle_permission(self, event: dict[str, object]) -> None:
        """Auto-approve any permission.asked event."""
        unwrapped = self._unwrap(event)
        if unwrapped.get("type") != "permission.asked":
            return
        properties = unwrapped.get("properties")
        if not isinstance(properties, dict):
            return
        properties = cast(dict[str, object], properties)
        request_id = properties.get("id")
        if not isinstance(request_id, str):
            return
        try:
            _http_request(
                "POST",
                f"{self._base_url}/permission/{request_id}/reply",
                body={"reply": "always"},
                timeout=5.0,
            )
        except Exception:
            pass

    def _session_matches_root(self, session: dict[str, object], root: Path) -> bool:
        expected_root = str(root.resolve())
        candidate_keys = ("directory", "cwd", "root", "worktree", "path")
        candidates = [
            str(Path(value).resolve())
            for key in candidate_keys
            if isinstance(value := session.get(key), str) and value
        ]
        return not candidates or expected_root in candidates

    def _tool_detail(self, tool_name: str, input_obj: object) -> str:
        return _tool_detail(
            tool_name,
            input_obj,
            cwd_hint=getattr(self, "_cwd_hint", ""),
        )

    def _unwrap(self, event: dict[str, object]) -> dict[str, object]:
        """Unwrap the {directory, payload} envelope from /global/event."""
        return _unwrap_event(event)

    def _session_status_type(
        self, event: dict[str, object], session_id: str
    ) -> str | None:
        event = self._unwrap(event)
        if event.get("type") != "session.status":
            return None
        properties = event.get("properties")
        if not isinstance(properties, dict):
            return None
        properties = cast(dict[str, object], properties)
        sid = properties.get("sessionID") or properties.get("session_id")
        if sid != session_id:
            return None
        status = properties.get("status")
        if isinstance(status, dict):
            status = cast(dict[str, object], status)
            status = status.get("type")
        return status if isinstance(status, str) else None

    def _render_event(
        self,
        event: dict[str, object],
        session_id: str,
        renderer: SavedLogRenderer,
    ) -> tuple[str, bool]:
        """Return (text_to_print, force_newline_after)."""
        del session_id
        return renderer.render_event(event)
