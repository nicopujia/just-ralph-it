import json
from dataclasses import replace
from pathlib import Path

from .errors import JriError
from .models import ProcessState, State
from .tasks import validate_state_payload


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> State:
        if not self.path.exists():
            return State()

        text = self.path.read_text(encoding="utf-8")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise JriError(f"state.json is corrupted: {exc}") from exc
        if not isinstance(payload, dict):
            raise JriError("state.json must contain an object")
        try:
            validate_state_payload(payload)
        except ValueError as exc:
            raise JriError(f"state.json has invalid content: {exc}") from exc
        return State.from_payload(payload)

    def save(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(state.to_payload(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def initialize(self) -> None:
        self.save(State())

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

    def mark_iteration_started(self, *, started_at: int) -> None:
        state = self.load()
        self.save(replace(state, started_at=started_at))

    def mark_iteration_finished(
        self, *, iteration_number: int, finished_at: int
    ) -> None:
        state = self.load()
        self.save(
            replace(
                state,
                iteration_number=iteration_number,
                started_at=None,
                finished_at=finished_at,
            )
        )
