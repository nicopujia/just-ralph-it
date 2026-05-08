import json
import os
from dataclasses import replace
from pathlib import Path
from typing import cast

from .errors import JriError
from .models import AttemptState, ProcessState, PromotionRecord, State
from .tasks import validate_state_payload


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.bak")

    def load(self) -> State:
        if not self.path.exists():
            return State()

        try:
            return self._load_path(self.path)
        except JriError as exc:
            return self._load_backup(primary_error=exc)

    def save(self, state: State) -> None:
        text = json.dumps(state.to_payload(), indent=2, sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_text_atomically(self.path, text)
        self._write_text_atomically(self.backup_path, text)

    def initialize(self, *, branch: str | None = None) -> None:
        self.save(State(branch=branch))

    def clear_process(self) -> None:
        state = self.load()
        self.save(replace(state, process=None))

    def save_process(
        self,
        *,
        loop_pid: int | None,
        child_pid: int | None,
        log_path: Path | None,
        detached: bool,
    ) -> None:
        state = self.load()
        process = ProcessState(
            loop_pid=loop_pid,
            child_pid=child_pid,
            log_path=str(log_path) if log_path is not None else None,
            detached=detached,
        )
        self.save(replace(state, process=process))

    def save_session(self, session_id: str | None) -> None:
        state = self.load()
        self.save(replace(state, session=session_id))

    def save_promotion(self, promotion: PromotionRecord) -> None:
        state = self.load()
        self.save(replace(state, promotion=promotion))

    def clear_promotion(self) -> None:
        state = self.load()
        self.save(replace(state, promotion=None))

    def start_attempt(self, attempt: AttemptState) -> None:
        state = self.load()
        self.save(
            replace(
                state,
                active_attempt=attempt,
                attempts=[*state.attempts, attempt],
            )
        )

    def save_active_attempt(self, attempt: AttemptState) -> None:
        state = self.load()
        attempts = [
            attempt if existing.number == attempt.number else existing
            for existing in state.attempts
        ]
        if not any(existing.number == attempt.number for existing in state.attempts):
            attempts.append(attempt)
        self.save(replace(state, active_attempt=attempt, attempts=attempts))

    def clear_active_attempt(self) -> None:
        state = self.load()
        self.save(replace(state, active_attempt=None))

    def mark_task_started(self, *, task_slug: str, started_at: int) -> None:
        state = self.load()
        self.save(replace(state, current_task=task_slug, started_at=started_at))

    def mark_task_finished(self, *, task_slug: str, finished_at: int) -> None:
        state = self.load()
        self.save(
            replace(
                state,
                started_at=None,
                finished_at=finished_at,
                current_task=None,
            )
        )

    def _load_path(self, path: Path) -> State:
        text = path.read_text(encoding="utf-8")
        return self._state_from_text(text, file_label=path.name)

    def _state_from_text(self, text: str, *, file_label: str) -> State:
        try:
            payload: object = json.loads(text)
        except json.JSONDecodeError as exc:
            raise JriError(f"{file_label} is corrupted: {exc}") from exc
        if not isinstance(payload, dict):
            raise JriError(f"{file_label} must contain an object")
        try:
            state_payload = cast(dict[str, object], payload)
            validate_state_payload(state_payload)
        except ValueError as exc:
            raise JriError(f"{file_label} has invalid content: {exc}") from exc
        return State.from_payload(state_payload)

    def _load_backup(self, *, primary_error: JriError) -> State:
        if not self.backup_path.exists():
            raise primary_error

        backup_text = self.backup_path.read_text(encoding="utf-8")
        try:
            state = self._state_from_text(
                backup_text,
                file_label=self.backup_path.name,
            )
        except JriError as exc:
            raise JriError(
                f"{primary_error}. Backup recovery from "
                f"{self.backup_path.name} failed: {exc}"
            ) from exc

        try:
            self._write_text_atomically(self.path, backup_text)
        except OSError:
            pass
        return state

    def _write_text_atomically(self, path: Path, text: str) -> None:
        temp_path = self._temp_path_for(path)
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        self._fsync_directory(path.parent)

    def _temp_path_for(self, path: Path) -> Path:
        return path.with_name(f".{path.name}.tmp")

    def _fsync_directory(self, directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
