import json
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

from .errors import JriError
from .models import OpenCodeRunResult, Outcome
from .ui import trim_tool_output

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
                    is_thinking = isinstance(event, dict) and _is_thinking_event(
                        event
                    )
                    show = terminal_text and not is_tool and not is_thinking
                    if show:
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
