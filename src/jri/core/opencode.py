import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

from .errors import JriError
from .models import OpenCodeRunResult, Outcome

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


def _parse_event_line(line: str) -> tuple[dict[str, object] | None, str | None]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None, line

    if not isinstance(payload, dict):
        return None, None

    return payload, _text_event_text(payload) or _tool_use_text(payload)


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


def _finalize_outcome(outcome: Outcome | None, *, context: str) -> Outcome:
    if outcome is not None:
        return outcome
    print(
        f"missing JRI outcome marker for {context}; treating run as failed",
        file=sys.stderr,
    )
    return "failed"


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
    ) -> OpenCodeRunResult:
        command = [self.binary, "run", "--format", "json", "--agent", "ralph"]
        if self.model:
            command.extend(["-m", self.model])
        command.append(prompt)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        session_id: str | None = None
        last_outcome: Outcome | None = None
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

            try:
                assert process.stdout is not None
                last_terminal_char = "\n"
                for line in process.stdout:
                    log_file.write(line)
                    log_file.flush()
                    event, terminal_text = _parse_event_line(line)
                    if terminal_text is not None:
                        last_outcome = _detect_outcome(terminal_text, last_outcome)
                    if terminal_text:
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
                    returncode = process.wait(timeout=14400)
                except subprocess.TimeoutExpired:
                    print(
                        "opencode process timed out after 4 hours",
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

        return OpenCodeRunResult(
            returncode=returncode,
            session_id=session_id,
            outcome=_finalize_outcome(last_outcome, context="Ralph run"),
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
