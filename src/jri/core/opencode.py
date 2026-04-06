import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import cast

from .errors import JriError
from .models import OpenCodeRunResult, Outcome
from .ui import DIM, _s, trim_tool_output

_COMPLETED_MARKER = "<!-- JRI:COMPLETED -->"
_FAILED_MARKER = "<!-- JRI:FAILED -->"
_NEEDS_HUMAN_MARKER = "<!-- JRI:NEEDS_HUMAN -->"
_OUTCOME_MARKERS: tuple[tuple[str, Outcome], ...] = (
    (_COMPLETED_MARKER, "completed"),
    (_FAILED_MARKER, "failed"),
    (_NEEDS_HUMAN_MARKER, "needs human"),
)


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


def _detect_outcome(text: str, current: Outcome | None) -> Outcome | None:
    """Update outcome if *text* contains a JRI marker. Last signal wins."""
    latest_match = current
    latest_index = -1
    for marker, outcome in _OUTCOME_MARKERS:
        marker_index = text.rfind(marker)
        if marker_index > latest_index:
            latest_index = marker_index
            latest_match = outcome
    return latest_match


def _finalize_outcome(
    outcome: Outcome | None, *, context: str
) -> tuple[Outcome, list[str]]:
    if outcome is not None:
        return outcome, []
    warning = f"missing JRI outcome marker for {context}; treating run as failed"
    print(warning, file=sys.stderr)
    return "failed", [warning]


class OpenCodeClient:
    def __init__(self, *, binary: str = "opencode", model: str | None = None) -> None:
        self.binary = binary
        self.model = model

    def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
        try:
            result = subprocess.run(
                [self.binary, "session", "list", "--format", "json", "-n", str(limit)],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as err:
            raise JriError(
                f"could not find `{self.binary}` — is OpenCode installed?"
            ) from err
        if result.returncode != 0:
            return []
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def launch_chat(
        self, *, root: Path, session_id: str | None, extra_args: list[str]
    ) -> int:
        command = [self.binary, str(root), "--agent", "interrogator"]
        if session_id:
            command.extend(["--session", session_id])
        command.extend(extra_args)
        try:
            return subprocess.run(command, cwd=root, check=False).returncode
        except FileNotFoundError as err:
            raise JriError(
                f"could not find `{self.binary}` — is OpenCode installed?"
            ) from err

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        on_start: Callable[[int], None] | None = None,
        timeout: int | None = None,
    ) -> OpenCodeRunResult:
        command = [self.binary, "run", "--format", "json", "--agent", "ralph"]
        if self.model:
            command.extend(["-m", self.model])
        command.append(prompt)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        session_id: str | None = None
        last_outcome: Outcome | None = None
        timed_out = False
        with log_path.open("a", encoding="utf-8") as log_file:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
            except FileNotFoundError as err:
                raise JriError(
                    f"could not find `{self.binary}` — is OpenCode installed?"
                ) from err
            if on_start is not None:
                on_start(process.pid)

            def _watchdog() -> None:
                nonlocal timed_out
                timed_out = True
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

            timer: threading.Timer | None = None
            if timeout is not None and timeout > 0:
                timer = threading.Timer(timeout, _watchdog)
                timer.start()

            try:
                assert process.stdout is not None
                last_terminal_char = "\n"
                for line in process.stdout:
                    log_file.write(line)
                    log_file.flush()
                    event, terminal_text, is_tool = _parse_event_line(line)
                    if terminal_text is not None:
                        last_outcome = _detect_outcome(terminal_text, last_outcome)
                    is_thinking = isinstance(event, dict) and _is_thinking_event(event)
                    show = terminal_text and not is_tool and not is_thinking
                    if show:
                        assert terminal_text is not None
                        sys.stdout.write(terminal_text)
                        sys.stdout.flush()
                        last_terminal_char = terminal_text[-1]
                    elif event is None and line:
                        last_terminal_char = line[-1]

                    if (
                        isinstance(event, dict)
                        and event.get("type") == "step_finish"
                        and last_terminal_char != "\n"
                    ):
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        last_terminal_char = "\n"

                    if not isinstance(event, dict):
                        continue
                    if session_id is None:
                        candidate = event.get("sessionID")
                        if isinstance(candidate, str):
                            session_id = candidate
                try:
                    returncode = process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    print(
                        "opencode process still alive 30s after stdout closed",
                        file=sys.stderr,
                    )
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    returncode = -1
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise
            finally:
                if timer is not None:
                    timer.cancel()

        if timed_out:
            msg = f"opencode process killed after {timeout}s timeout"
            print(msg, file=sys.stderr)
            return OpenCodeRunResult(
                returncode=-1,
                session_id=session_id,
                outcome="timeout",
                warnings=[msg],
            )
        outcome, warnings = _finalize_outcome(last_outcome, context="Ralph run")
        return OpenCodeRunResult(
            returncode=returncode,
            session_id=session_id,
            outcome=outcome,
            warnings=warnings,
        )

    def export_session(self, session_id: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [self.binary, "export", session_id],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise JriError(
                result.stderr.strip() or f"failed to export session {session_id}"
            )
        destination.write_text(result.stdout, encoding="utf-8")


# ---------------------------------------------------------------------------
# HTTP server-based client (opencode serve)
# ---------------------------------------------------------------------------


_SERVER_HEALTH_TIMEOUT = 30.0
_SERVER_HEALTH_INTERVAL = 0.25


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
        port: int = 4096,
        model: str | None = None,
    ) -> None:
        self.binary = binary
        self.port = port
        self.model = model
        self._process: subprocess.Popen[bytes] | None = None
        self._base_url = f"http://127.0.0.1:{port}"

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
        try:
            self._process = subprocess.Popen(
                [self.binary, "serve", "--port", str(self.port)],
                cwd=str(cwd) if cwd is not None else None,
                env=merged_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError as err:
            raise JriError(
                f"could not find `{self.binary}` — is OpenCode installed?"
            ) from err

        deadline = time.monotonic() + _SERVER_HEALTH_TIMEOUT
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self._process = None
                raise JriError("opencode serve exited before becoming healthy")
            if self.is_healthy():
                return
            time.sleep(_SERVER_HEALTH_INTERVAL)
        self.stop()
        raise JriError(
            f"opencode serve did not become healthy within {_SERVER_HEALTH_TIMEOUT}s"
        )

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        if process.poll() is not None:
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
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

    # -- ralph task --------------------------------------------------------

    def run_ralph_task(
        self,
        *,
        root: Path,
        prompt: str,
        log_path: Path,
        outcome_path: Path,
        on_start: Callable[[int], None] | None = None,
        timeout: int | None = None,
    ) -> OpenCodeRunResult:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        if outcome_path.exists():
            outcome_path.unlink()

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
        deadline = time.monotonic() + timeout if timeout and timeout > 0 else None

        # 3. Send prompt
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
            stop_event.set()
            self._delete_session(session_id)
            raise JriError(f"failed to start ralph prompt: {err}") from err
        if p_status >= 400:
            stop_event.set()
            self._delete_session(session_id)
            raise JriError(
                f"failed to start ralph prompt (HTTP {p_status}): "
                f"{p_body.decode('utf-8', errors='replace')}"
            )

        # 4. Drive event loop
        last_terminal_char = "\n"
        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                while True:
                    if deadline is not None and time.monotonic() > deadline:
                        timed_out = True
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

                    text_to_print, newline_after = self._render_event(event, session_id)
                    if text_to_print:
                        sys.stdout.write(text_to_print)
                        sys.stdout.flush()
                        last_terminal_char = text_to_print[-1]
                    if newline_after and last_terminal_char != "\n":
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        last_terminal_char = "\n"

                    if self._is_session_idle(event, session_id):
                        break
        finally:
            stop_event.set()

        # 5. Read outcome
        outcome: Outcome
        warnings: list[str] = []
        if timed_out:
            outcome = "timeout"
            msg = f"opencode prompt killed after {timeout}s timeout"
            print(msg, file=sys.stderr)
            warnings.append(msg)
        elif outcome_path.exists():
            raw = outcome_path.read_text(encoding="utf-8").strip()
            if raw == "completed":
                outcome = "completed"
            elif raw == "needs_human":
                outcome = "needs human"
            elif raw == "failed":
                outcome = "failed"
            else:
                warning = f"unrecognized JRI outcome `{raw}`; treating run as failed"
                print(warning, file=sys.stderr)
                warnings.append(warning)
                outcome = "failed"
        else:
            warning = "missing JRI outcome marker for Ralph run; treating run as failed"
            print(warning, file=sys.stderr)
            warnings.append(warning)
            outcome = "failed"

        # 6. Cleanup session
        self._delete_session(session_id)

        return OpenCodeRunResult(
            returncode=0 if not timed_out else -1,
            session_id=session_id,
            outcome=outcome,
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

    def _is_session_idle(self, event: dict[str, object], session_id: str) -> bool:
        if event.get("type") != "session.status":
            return False
        properties = event.get("properties")
        if not isinstance(properties, dict):
            return False
        sid = properties.get("sessionID") or properties.get("session_id")
        if sid != session_id:
            return False
        status = properties.get("status")
        if isinstance(status, dict):
            return status.get("type") == "idle"
        return status == "idle"

    def _render_event(
        self, event: dict[str, object], session_id: str
    ) -> tuple[str, bool]:
        """Return (text_to_print, force_newline_after)."""
        etype = event.get("type")
        if etype != "message.part.updated":
            return "", False
        properties = event.get("properties")
        if not isinstance(properties, dict):
            return "", False
        part = properties.get("part")
        if not isinstance(part, dict):
            return "", False
        part_type = part.get("type")
        if part_type == "reasoning":
            return "", False
        if part_type == "text":
            delta = properties.get("delta")
            if isinstance(delta, str) and delta:
                return delta, False
            text = part.get("text")
            if isinstance(text, str) and text:
                return text, False
            return "", False
        if part_type == "tool":
            state = part.get("state")
            if not isinstance(state, dict):
                return "", False
            status = state.get("status")
            if status == "running":
                tool_name = part.get("tool") or state.get("tool") or "tool"
                title = ""
                input_obj = state.get("input")
                if isinstance(input_obj, dict):
                    raw_title = input_obj.get("title")
                    if isinstance(raw_title, str):
                        title = raw_title
                if not title:
                    metadata = state.get("metadata")
                    if isinstance(metadata, dict):
                        raw_title = metadata.get("title")
                        if isinstance(raw_title, str):
                            title = raw_title
                label = f"⚙ {tool_name}"
                if title:
                    label = f"{label}: {title}"
                return _s(label, DIM) + "\n", False
            return "", False
        return "", False
