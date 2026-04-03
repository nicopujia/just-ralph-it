import os
import shutil
import signal
import subprocess
import sys
import time
from importlib.resources import files
from pathlib import Path
from types import FrameType
from typing import Any

from .errors import HaltRequested, JriError
from .git import GitRepo
from .models import State, Task
from .opencode import OpenCodeClient
from .paths import JriPaths
from .state import StateStore
from .tasks import list_tasks, move_task, select_next_task

_INIT_COMMIT_PATHS = (
    ".jri",
    ".gitignore",
)
_UPGRADE_COMMIT_PATHS = (
    ".jri/.gitignore",
    ".gitignore",
)
_MANAGED_AGENT_FILENAMES = ("interrogator.md", "ralph.md")
_MANAGED_AGENT_PATHS = tuple(
    f".opencode/agents/{name}" for name in _MANAGED_AGENT_FILENAMES
)
_TRACKED_TASK_DIRS = ("draft", "todo", "doing", "done")


class JriService:
    def __init__(
        self, root: Path, *, opencode_client: OpenCodeClient | None = None
    ) -> None:
        self.root = root.resolve()
        self.paths = JriPaths(self.root)
        self.git = GitRepo(self.root)
        self.state_store = StateStore(self.paths.state_path)
        self.opencode_client = opencode_client or OpenCodeClient()
        self._halt_requested = False

    def init(self, *, force: bool, commit_message: str) -> None:
        self.git.ensure_repo()
        if self.paths.jri_dir.exists():
            if not force:
                raise JriError("project is already initialized")
            shutil.rmtree(self.paths.jri_dir)

        created_files = self._create_scaffold()
        commit_paths = list(_INIT_COMMIT_PATHS)
        commit_paths.extend(str(path.relative_to(self.root)) for path in created_files)
        self.git.commit_paths_if_needed(commit_message, commit_paths)

    def upgrade(self, *, commit_message: str) -> None:
        self.ensure_initialized()
        self._write_managed_files()
        self.git.commit_upgrade_if_needed(
            commit_message,
            managed_paths=list(_UPGRADE_COMMIT_PATHS),
            untracked_paths=list(_MANAGED_AGENT_PATHS),
        )

    def chat(self, extra_args: list[str]) -> int:
        self.ensure_initialized()
        before = {
            session_id
            for session in self.opencode_client.list_sessions(root=self.root)
            if isinstance((session_id := session.get("id")), str)
        }
        state = self.state_store.load()
        returncode = self.opencode_client.launch_chat(
            root=self.root,
            session_id=state.session,
            extra_args=extra_args,
        )
        if state.session is None:
            after = self.opencode_client.list_sessions(root=self.root)
            session_id = self._detect_latest_session(before, after)
            if session_id is not None:
                self.state_store.save_session(session_id)
        return returncode

    def start(
        self,
        *,
        iterations: int | None = None,
        detached: bool = False,
        model: str | None = None,
    ) -> int:
        self.ensure_initialized()
        if detached:
            return self._start_detached(iterations, model)

        previous_model = self.opencode_client.model
        self.opencode_client.model = model
        try:
            return self._run_loop(iterations)
        finally:
            self.opencode_client.model = previous_model

    def stop(self, reason: str | None = None) -> None:
        self.ensure_initialized()
        self.paths.signals_dir.mkdir(parents=True, exist_ok=True)
        content = f"{reason}\n" if reason else ""
        self.paths.stop_signal_path.write_text(content, encoding="utf-8")

    def halt(self) -> None:
        self.ensure_initialized()
        state = self.state_store.load()
        if state.process is None or state.process.loop_pid is None:
            raise JriError("no Ralph process is currently tracked")

        seen: set[int] = set()
        for pid in (state.process.child_pid, state.process.loop_pid):
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
        self.state_store.clear_process()

    def reset(self) -> None:
        self.ensure_initialized()
        self.git.ensure_clean()
        self.git.checkout("main")
        state = self.state_store.load()
        iteration_number = state.iteration_number
        if iteration_number < 1:
            raise JriError("no successful iteration exists yet")
        self.git.reset_hard(f"jri/{iteration_number}")
        self.state_store.save(
            State(
                iteration_number=iteration_number,
                finished_at=state.finished_at,
                session=state.session,
            )
        )

    def ensure_initialized(self) -> None:
        self.git.ensure_repo()
        if not self.paths.jri_dir.exists():
            raise JriError("project is not initialized; run `jri init`")

    def _create_scaffold(self) -> list[Path]:
        created_files: list[Path] = []
        readme_path = self.paths.readme_path
        if not readme_path.exists():
            readme_path.write_text("", encoding="utf-8")
            created_files.append(readme_path)

        for status in _TRACKED_TASK_DIRS:
            directory = self.paths.task_dir(status)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / ".gitkeep").write_text("", encoding="utf-8")

        self.paths.signals_dir.mkdir(parents=True, exist_ok=True)
        self.paths.ralph_logs_dir.mkdir(parents=True, exist_ok=True)
        self.paths.external_logs_dir.mkdir(parents=True, exist_ok=True)
        self.paths.external_opencode_dir.mkdir(parents=True, exist_ok=True)
        self._write_managed_files()
        self.state_store.initialize()
        return created_files

    def _write_managed_files(self) -> None:
        self.paths.gitignore_path.write_text(
            "logs/\nsignals/\nstate.json\n", encoding="utf-8"
        )
        _ensure_ignore_entries(self.paths.root_gitignore_path, _MANAGED_AGENT_PATHS)
        self.paths.opencode_agents_dir.mkdir(parents=True, exist_ok=True)
        for name in _MANAGED_AGENT_FILENAMES:
            (self.paths.opencode_agents_dir / name).write_text(
                _load_prompt(name),
                encoding="utf-8",
            )

    def _start_detached(self, iterations: int | None, model: str | None) -> int:
        state = self.state_store.load()
        if state.process and state.process.loop_pid:
            raise JriError("a Ralph process is already tracked")

        command = [sys.executable, "-m", "jri", "start"]
        if iterations is not None:
            command.extend(["-n", str(iterations)])
        if model is not None:
            command.extend(["--model", model])

        log_path = self.paths.ralph_log_path(
            self.state_store.load().iteration_number + 1, int(time.time())
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            command,
            cwd=self.root,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_file.close()
        self.state_store.save_process(
            loop_pid=process.pid,
            child_pid=None,
            log_path=log_path,
            detached=True,
        )
        return 0

    def _run_loop(self, iterations: int | None) -> int:
        if list_tasks(self.paths.task_dir("doing")):
            raise JriError("a task is already in progress")
        self.git.ensure_clean()
        self.git.ensure_main()
        if self.paths.stop_signal_path.exists():
            self.paths.stop_signal_path.unlink()

        completed = 0
        old_handlers = self._install_signal_handlers()
        try:
            while iterations is None or completed < iterations:
                if self._halt_requested:
                    raise HaltRequested("Ralph halt requested")

                todo_tasks = list_tasks(self.paths.task_dir("todo"))
                done_tasks = list_tasks(self.paths.task_dir("done"))
                doing_tasks = list_tasks(self.paths.task_dir("doing"))
                next_task = select_next_task(
                    todo_tasks,
                    done_slugs={task.slug for task in done_tasks},
                    doing_tasks=doing_tasks,
                )
                if next_task is None:
                    break

                self._run_iteration(next_task)
                completed += 1

                if self.paths.stop_signal_path.exists():
                    self.paths.stop_signal_path.unlink()
                    break
        finally:
            self._restore_signal_handlers(old_handlers)
            self.state_store.clear_process()

        return completed

    def _run_iteration(self, task: Task) -> None:
        state = self.state_store.load()
        next_iteration = state.iteration_number + 1
        started_at = int(time.time())
        log_path = self.paths.ralph_log_path(next_iteration, started_at)
        branch = f"ralph/{next_iteration}/{task.slug}"

        self.state_store.mark_iteration_started(started_at=started_at)
        self.git.checkout_new_branch(branch)
        doing_task = move_task(task, self.paths.task_dir("doing"))
        self.git.commit_all_if_needed(f"jri start: begin {task.slug}")
        self.state_store.save_process(
            loop_pid=os.getpid(),
            child_pid=None,
            log_path=log_path,
            detached=False,
        )

        result = self.opencode_client.run_ralph_task(
            root=self.root,
            prompt=(
                f"Solve `{doing_task.path.relative_to(self.root)}`. Commit frequently."
            ),
            log_path=log_path,
            on_start=lambda child_pid: self.state_store.save_process(
                loop_pid=os.getpid(),
                child_pid=child_pid,
                log_path=log_path,
                detached=False,
            ),
        )
        if result.returncode != 0:
            raise JriError(f"OpenCode exited with status {result.returncode}")

        if result.session_id is not None:
            export_path = self.paths.external_opencode_dir / f"{result.session_id}.json"
            self.opencode_client.export_session(result.session_id, export_path)

        self.git.commit_all_if_needed(f"ralph: finalize {task.slug}")
        self.git.checkout("main")
        self.git.merge_ff_only(branch)

        if not doing_task.path.exists():
            relative_path = doing_task.path.relative_to(self.root)
            raise JriError(f"task file `{relative_path}` disappeared during Ralph run")
        move_task(doing_task, self.paths.task_dir("done"))
        self.git.commit_all_if_needed(f"jri start: complete {task.slug}")
        self.git.create_tag(f"jri/{next_iteration}")

        if self.git.has_remote():
            self.git.push_iteration(branch=branch, tag=f"jri/{next_iteration}")

        self.state_store.mark_iteration_finished(
            iteration_number=next_iteration,
            finished_at=int(time.time()),
        )

    def _install_signal_handlers(self) -> dict[signal.Signals, Any]:
        previous: dict[signal.Signals, Any] = {}

        def handler(_signum: int, _frame: FrameType | None) -> None:
            self._halt_requested = True
            raise HaltRequested("Ralph halt requested")

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, handler)
        return previous

    def _restore_signal_handlers(self, handlers: dict[signal.Signals, Any]) -> None:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)

    def _detect_latest_session(
        self,
        before: set[str],
        sessions: list[dict[str, object]],
    ) -> str | None:
        for session in sessions:
            session_id = session.get("id")
            directory = session.get("directory")
            if isinstance(session_id, str) and isinstance(directory, str):
                if Path(directory).resolve() == self.root and session_id not in before:
                    return session_id
        for session in sessions:
            session_id = session.get("id")
            directory = session.get("directory")
            if (
                isinstance(session_id, str)
                and isinstance(directory, str)
                and Path(directory).resolve() == self.root
            ):
                return session_id
        return None


def _load_prompt(name: str) -> str:
    return files("jri.core.agents").joinpath(name).read_text(encoding="utf-8")


def _ensure_ignore_entry(path: Path, entry: str) -> None:
    _ensure_ignore_entries(path, (entry,))


def _ensure_ignore_entries(path: Path, entries: tuple[str, ...]) -> None:
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    missing_entries = [entry for entry in entries if entry not in lines]
    if not missing_entries:
        return

    if lines and lines[-1] != "":
        lines.append("")
    lines.extend(missing_entries)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
