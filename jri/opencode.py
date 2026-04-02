from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from .errors import JriError
from .models import OpenCodeRunResult


class OpenCodeClient:
    def __init__(self, *, binary: str = "opencode", model: str | None = None) -> None:
        self.binary = binary
        self.model = model

    def list_sessions(self, *, root: Path, limit: int = 20) -> list[dict[str, object]]:
        result = subprocess.run(
            [self.binary, "session", "list", "--format", "json", "-n", str(limit)],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout or "[]")
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
        return subprocess.run(command, cwd=root, check=False).returncode

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
        with log_path.open("a", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                command,
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            if on_start is not None:
                on_start(process.pid)

            try:
                assert process.stdout is not None
                for line in process.stdout:
                    log_file.write(line)
                    log_file.flush()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if session_id is None and isinstance(event, dict):
                        candidate = event.get("sessionID")
                        if isinstance(candidate, str):
                            session_id = candidate
                returncode = process.wait()
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise

        return OpenCodeRunResult(returncode=returncode, session_id=session_id)

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
