import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from types import FrameType
from typing import Any

from .errors import HaltRequested, JriError
from .git import GitRepo
from .models import AttemptState, Outcome, ProcessState, State, Task, TaskMetadata
from .opencode import OpenCodeClient
from .paths import JriPaths
from .state import StateStore
from .tasks import dump_task, list_tasks, move_task, parse_task_file, select_next_task

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
_MAX_TASK_TITLE_LENGTH = 50
_MAX_FAILED_ATTEMPTS = 3


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
        self._recover_stale_start_state(mode="detached" if detached else "foreground")
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

        own_pgid = os.getpgrp()
        seen: set[int] = set()
        for pid in (state.process.child_pid, state.process.loop_pid):
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            try:
                pgid = os.getpgid(pid)
                if pgid != own_pgid:
                    os.killpg(pgid, signal.SIGTERM)
                    continue
            except (ProcessLookupError, PermissionError):
                pass
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
        self.state_store.clear_process()

    def status(self) -> dict[str, list[Task]]:
        self.ensure_initialized()
        try:
            return {
                status: list_tasks(self.paths.task_dir(status), git_repo=self.git)
                for status in _TRACKED_TASK_DIRS
            }
        except ValueError as exc:
            raise JriError(str(exc)) from exc

    def reset(self) -> None:
        self.ensure_initialized()
        self.git.ensure_clean()
        state = self.state_store.load()
        default = self.git.default_branch(hint=state.branch)
        self.git.checkout(default)
        iteration_number = state.iteration_number
        if iteration_number < 1:
            raise JriError("no successful iteration exists yet")
        self.git.reset_hard(f"jri/{iteration_number}")
        self.state_store.save(
            State(
                iteration_number=iteration_number,
                finished_at=state.finished_at,
                session=state.session,
                branch=state.branch,
                attempts=state.attempts,
            )
        )

    def ensure_initialized(self) -> None:
        self.git.ensure_repo()
        if not self.paths.jri_dir.exists():
            raise JriError("project is not initialized; run `jri init`")

    def _default_branch(self) -> str:
        return self.git.default_branch(hint=self.state_store.load().branch)

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
        self.state_store.initialize(branch=self.git.current_branch() or None)
        return created_files

    def _write_managed_files(self) -> None:
        self.paths.gitignore_path.write_text(
            "logs/\nsignals/\nstate.json\nstate.json.bak\n.state.json.tmp\n.state.json.bak.tmp\n",
            encoding="utf-8",
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
        try:
            doing = list_tasks(self.paths.task_dir("doing"), git_repo=self.git)
        except ValueError as exc:
            raise JriError(str(exc)) from exc
        if doing:
            raise JriError("a task is already in progress")
        self.git.ensure_clean()
        self.git.ensure_default_branch(hint=self.state_store.load().branch)
        self._ensure_initial_iteration_tag()
        if self.paths.stop_signal_path.exists():
            self.paths.stop_signal_path.unlink()

        completed = 0
        failed_slugs: set[str] = set()
        self._halt_requested = False
        old_handlers = self._install_signal_handlers()
        try:
            while iterations is None or completed < iterations:
                if self._halt_requested:
                    raise HaltRequested("Ralph halt requested")

                try:
                    todo_tasks = list_tasks(
                        self.paths.task_dir("todo"),
                        git_repo=self.git,
                    )
                    done_tasks = list_tasks(
                        self.paths.task_dir("done"),
                        git_repo=self.git,
                    )
                    doing_tasks = list_tasks(
                        self.paths.task_dir("doing"),
                        git_repo=self.git,
                    )
                except ValueError as exc:
                    raise JriError(str(exc)) from exc

                def _is_eligible(task: Task) -> bool:
                    if task.slug in failed_slugs:
                        return False
                    return self._count_failed_attempts(task.slug) < _MAX_FAILED_ATTEMPTS

                next_task = select_next_task(
                    [t for t in todo_tasks if _is_eligible(t)],
                    done_slugs={task.slug for task in done_tasks},
                    doing_tasks=doing_tasks,
                )
                if next_task is None:
                    break

                outcome = self._run_iteration(next_task)
                if outcome == "completed":
                    completed += 1
                elif outcome == "failed":
                    failed_slugs.add(next_task.slug)
                    if self._count_failed_attempts(next_task.slug) >= _MAX_FAILED_ATTEMPTS:
                        self._escalate_failed_task(next_task)
                elif outcome == "needs human":
                    failed_slugs.add(next_task.slug)

                if self.paths.stop_signal_path.exists():
                    self.paths.stop_signal_path.unlink()
                    break
        finally:
            self._restore_signal_handlers(old_handlers)
            self.state_store.clear_process()

        return completed

    def _run_iteration(self, task: Task) -> Outcome:
        state = self.state_store.load()
        next_iteration = state.iteration_number + 1
        started_at = int(time.time())
        log_path = self.paths.ralph_log_path(next_iteration, started_at)
        branch = f"ralph/{next_iteration}/{task.slug}"

        attempt = AttemptState(
            number=len(state.attempts) + 1,
            task_slug=task.slug,
            iteration_number=next_iteration,
            branch=branch,
            started_at=started_at,
            log_path=str(log_path),
        )
        self.state_store.start_attempt(attempt)
        self.state_store.mark_iteration_started(started_at=started_at)
        self.git.checkout_new_branch(branch)
        doing_task = move_task(task, self.paths.task_dir("doing"))
        self.git.commit_all_if_needed(f"jri start: begin {task.slug}")
        doing_task_baseline = doing_task.path.read_text(encoding="utf-8")
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
            self._recover_failed_iteration(doing_task, branch)
            self._finish_attempt(attempt, outcome="failed")
            raise JriError(f"OpenCode exited with status {result.returncode}")

        export_path = self._export_session_if_available(result.session_id)
        attempt = replace(attempt, session_id=result.session_id)
        self.state_store.save_active_attempt(attempt)

        if not doing_task.path.exists():
            self._recover_failed_iteration(doing_task, branch)
            self._finish_attempt(attempt, outcome="failed")
            relative_path = doing_task.path.relative_to(self.root)
            raise JriError(f"task file `{relative_path}` disappeared during Ralph run")

        try:
            self._ensure_promoted_task_pristine(
                doing_task,
                baseline=doing_task_baseline,
            )
        except JriError:
            self._recover_failed_iteration(doing_task, branch)
            self._finish_attempt(attempt, outcome="failed")
            raise

        if result.outcome == "needs human":
            self._recover_needs_human_iteration(
                doing_task,
                branch,
                log_path=log_path,
                session_id=result.session_id,
                export_path=export_path,
            )
            self._finish_attempt(attempt, outcome="needs human")
            return "needs human"

        if result.outcome == "failed":
            self._recover_failed_iteration(doing_task, branch)
            self._finish_attempt(attempt, outcome="failed")
            return "failed"

        default = self._default_branch()
        self.git.commit_all_if_needed(f"ralph: finalize {task.slug}")

        if (self.root / "Makefile").exists():
            try:
                check = subprocess.run(
                    ["make", "check"],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                )
            except FileNotFoundError:
                print("make: command not found", file=sys.stderr)
                self._recover_failed_iteration(doing_task, branch)
                self._finish_attempt(attempt, outcome="failed")
                return "failed"
            if check.returncode != 0:
                print(
                    f"make check failed for {task.slug}:\n{check.stderr}",
                    file=sys.stderr,
                )
                self._recover_failed_iteration(doing_task, branch)
                self._finish_attempt(attempt, outcome="failed")
                return "failed"

        self.git.checkout(default)
        self.git.merge_ff_only(branch)

        if not doing_task.path.exists():
            relative_path = doing_task.path.relative_to(self.root)
            raise JriError(f"task file `{relative_path}` disappeared during Ralph run")
        move_task(doing_task, self.paths.task_dir("done"))
        self.git.commit_all_if_needed(f"jri start: complete {task.slug}")
        self.git.create_tag(f"jri/{next_iteration}")

        if self.git.has_remote():
            self.git.push_iteration(branch=branch, tag=f"jri/{next_iteration}")

        finished_at = int(time.time())
        attempt = replace(attempt, finished_at=finished_at, outcome="completed")
        self.state_store.save_active_attempt(attempt)
        self.state_store.mark_iteration_finished(
            iteration_number=next_iteration,
            finished_at=finished_at,
        )
        self.state_store.clear_active_attempt()
        return "completed"

    def _ensure_initial_iteration_tag(self) -> None:
        state = self.state_store.load()
        if state.iteration_number == 0 and not self.git.has_tag("jri/0"):
            self.git.create_tag("jri/0")

    def _recover_stale_start_state(self, *, mode: str) -> None:
        state = self.state_store.load()
        try:
            doing_tasks = list_tasks(self.paths.task_dir("doing"), git_repo=self.git)
        except ValueError as exc:
            raise JriError(str(exc)) from exc

        if len(doing_tasks) > 1:
            raise JriError("multiple tasks are already in progress")

        process = state.process
        loop_pid = process.loop_pid if process is not None else None
        process_alive = loop_pid is not None and self._is_pid_alive(loop_pid)

        if process_alive:
            raise JriError("a Ralph process is already running")

        if doing_tasks:
            reason = (
                "dead-tracked-process" if loop_pid is not None else "no-tracked-process"
            )
            active_attempt = state.active_attempt
            if active_attempt is not None:
                if not self._attempt_matches_task(active_attempt, doing_tasks[0]):
                    raise JriError("active attempt does not match the task in progress")
                if self._attempt_completion_applied(active_attempt):
                    self._record_recovery(
                        mode=mode,
                        reason="resume-completed-attempt",
                        task_slug=doing_tasks[0].slug,
                        process=process,
                    )
                    self._complete_attempt(active_attempt, doing_task=doing_tasks[0])
                    return
            self._recover_stale_iteration(
                doing_tasks[0],
                mode=mode,
                reason=reason,
                process=process,
            )
            return

        if state.active_attempt is not None:
            active_attempt = state.active_attempt
            if self._attempt_completion_applied(active_attempt):
                self._record_recovery(
                    mode=mode,
                    reason="resume-completed-attempt",
                    task_slug=active_attempt.task_slug,
                    process=process,
                )
                self._complete_attempt(active_attempt, doing_task=None)
                return
            if active_attempt.outcome in {"failed", "needs human", "interrupted"}:
                self._reset_runtime_state()
                self.state_store.clear_active_attempt()
                return

        if process is not None:
            reason = (
                "dead-tracked-process" if loop_pid is not None else "missing-loop-pid"
            )
            self._record_recovery(
                mode=mode,
                reason=reason,
                task_slug=None,
                process=process,
            )
            self._mark_active_attempt_interrupted()
            self._reset_runtime_state()
            return

        if state.started_at is not None:
            self._record_recovery(
                mode=mode,
                reason="stale-iteration-state",
                task_slug=None,
                process=None,
            )
            self._mark_active_attempt_interrupted()
            self._reset_runtime_state()

    def _is_pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _recover_stale_iteration(
        self,
        doing_task: Task,
        *,
        mode: str,
        reason: str,
        process: ProcessState | None,
    ) -> None:
        default = self._default_branch()
        state = self.state_store.load()
        expected_branch = f"ralph/{state.iteration_number + 1}/{doing_task.slug}"
        current_branch = self.git.current_branch()

        if current_branch == default:
            if self.git.status_short():
                raise JriError("git working tree must be clean before stale recovery")
        elif current_branch == expected_branch:
            self.git.commit_all_if_needed(f"ralph: partial work on {doing_task.slug}")
            self.git.checkout(default)
        else:
            raise JriError(f"jri start must begin from the {default} branch")

        self.git.delete_branch(expected_branch)

        move_task(doing_task, self.paths.task_dir("todo"))
        self._record_recovery(
            mode=mode,
            reason=reason,
            task_slug=doing_task.slug,
            process=process,
        )
        self._mark_active_attempt_interrupted()
        self._reset_runtime_state()
        self.git.commit_all_if_needed(f"jri: recover {doing_task.slug} after stale run")

    def _reset_runtime_state(self) -> None:
        state = self.state_store.load()
        self.state_store.save(
            State(
                iteration_number=state.iteration_number,
                finished_at=state.finished_at,
                session=state.session,
                branch=state.branch,
                active_attempt=state.active_attempt,
                attempts=state.attempts,
            )
        )

    def _finish_attempt(self, attempt: AttemptState, *, outcome: str) -> None:
        finished_at = int(time.time())
        self.state_store.save_active_attempt(
            replace(attempt, finished_at=finished_at, outcome=outcome)
        )
        self.state_store.clear_active_attempt()

    def _mark_active_attempt_interrupted(self) -> None:
        state = self.state_store.load()
        if state.active_attempt is None:
            return
        self.state_store.save_active_attempt(
            replace(
                state.active_attempt,
                finished_at=state.active_attempt.finished_at or int(time.time()),
                outcome="interrupted",
            )
        )
        self.state_store.clear_active_attempt()

    def _attempt_matches_task(self, attempt: AttemptState, task: Task) -> bool:
        return (
            attempt.task_slug == task.slug
            and attempt.iteration_number == self.state_store.load().iteration_number + 1
            and attempt.branch == f"ralph/{attempt.iteration_number}/{task.slug}"
        )

    def _attempt_completion_applied(self, attempt: AttemptState) -> bool:
        if attempt.outcome == "completed":
            return True
        if not self.git.has_local_branch(attempt.branch):
            return False
        return self.git.is_ancestor(attempt.branch, self._default_branch())

    def _complete_attempt(
        self,
        attempt: AttemptState,
        *,
        doing_task: Task | None,
    ) -> None:
        default = self._default_branch()
        current_branch = self.git.current_branch()
        if current_branch == attempt.branch:
            if self.git.status_short():
                self.git.commit_all_if_needed(
                    f"ralph: partial work on {attempt.task_slug}"
                )
            self.git.checkout(default)
        elif current_branch != default:
            raise JriError(f"jri start must begin from the {default} branch")

        if doing_task is not None and doing_task.path.exists():
            move_task(doing_task, self.paths.task_dir("done"))
        self.git.commit_all_if_needed(f"jri start: complete {attempt.task_slug}")
        tag = f"jri/{attempt.iteration_number}"
        if not self.git.has_tag(tag):
            self.git.create_tag(tag)
        if self.git.has_remote() and self.git.has_local_branch(attempt.branch):
            self.git.push_iteration(branch=attempt.branch, tag=tag)

        finished_at = attempt.finished_at or int(time.time())
        self.state_store.save_active_attempt(
            replace(attempt, finished_at=finished_at, outcome="completed")
        )
        self.state_store.mark_iteration_finished(
            iteration_number=attempt.iteration_number,
            finished_at=finished_at,
        )
        self.state_store.clear_process()
        self.state_store.clear_active_attempt()

    def _record_recovery(
        self,
        *,
        mode: str,
        reason: str,
        task_slug: str | None,
        process: ProcessState | None,
    ) -> None:
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        loop_pid = process.loop_pid if process is not None else None
        child_pid = process.child_pid if process is not None else None
        detached = process.detached if process is not None else False
        log_path = process.log_path if process is not None else None
        line = " ".join(
            (
                timestamp,
                "event=stale-run-recovery",
                f"mode={mode}",
                f"task={task_slug or '-'}",
                f"reason={reason}",
                f"loop_pid={loop_pid if loop_pid is not None else '-'}",
                f"child_pid={child_pid if child_pid is not None else '-'}",
                f"detached={'true' if detached else 'false'}",
                f"log_path={log_path or '-'}",
            )
        )
        self.paths.recovery_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.recovery_log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _recover_failed_iteration(self, doing_task: Task, branch: str) -> None:
        try:
            default = self._default_branch()
            self.git.commit_all_if_needed(f"ralph: partial work on {doing_task.slug}")
            self.git.checkout(default)
            self.git.delete_branch(branch)
            if doing_task.path.exists():
                move_task(doing_task, self.paths.task_dir("todo"))
                self.git.commit_all_if_needed(
                    f"jri: recover {doing_task.slug} after failed iteration"
                )
            self._reset_runtime_state()
        except Exception:
            pass  # best-effort recovery; don't mask the original error

    def _count_failed_attempts(self, task_slug: str) -> int:
        state = self.state_store.load()
        return sum(
            1
            for attempt in state.attempts
            if attempt.task_slug == task_slug and attempt.outcome == "failed"
        )

    def _last_attempt_log_path(self, task_slug: str) -> Path:
        state = self.state_store.load()
        for attempt in reversed(state.attempts):
            if attempt.task_slug == task_slug and attempt.log_path is not None:
                return Path(attempt.log_path)
        return self.paths.ralph_log_path(0, 0)

    def _last_attempt_session_id(self, task_slug: str) -> str | None:
        state = self.state_store.load()
        for attempt in reversed(state.attempts):
            if attempt.task_slug == task_slug:
                return attempt.session_id
        return None

    def _escalate_failed_task(self, task: Task) -> None:
        todo_path = self.paths.task_path("todo", task.slug)
        if not todo_path.exists():
            return
        todo_task = parse_task_file(todo_path)
        log_path = self._last_attempt_log_path(todo_task.slug)
        session_id = self._last_attempt_session_id(todo_task.slug)
        export_path = self._export_session_if_available(session_id)
        human_task = self._create_needs_human_task(
            todo_task,
            log_path=log_path,
            session_id=session_id,
            export_path=export_path,
        )
        blocked_task = self._block_task_on_dependency(todo_task, human_task.slug)
        self._write_task_file(blocked_task)
        self.git.commit_all_if_needed(
            f"jri: escalate {task.slug} after {_MAX_FAILED_ATTEMPTS} failed attempts"
        )

    def _recover_needs_human_iteration(
        self,
        doing_task: Task,
        branch: str,
        *,
        log_path: Path,
        session_id: str | None,
        export_path: Path | None,
    ) -> None:
        try:
            default = self._default_branch()
            self.git.commit_all_if_needed(f"ralph: partial work on {doing_task.slug}")
            self.git.checkout(default)
            self.git.delete_branch(branch)
            todo_path = self.paths.task_path("todo", doing_task.slug)
            if todo_path.exists():
                todo_task = parse_task_file(todo_path)
                human_task = self._create_needs_human_task(
                    todo_task,
                    log_path=log_path,
                    session_id=session_id,
                    export_path=export_path,
                )
                blocked_task = self._block_task_on_dependency(
                    todo_task, human_task.slug
                )
                self._write_task_file(blocked_task)
                self.git.commit_all_if_needed(
                    f"jri: escalate {doing_task.slug} for human help"
                )
            self._reset_runtime_state()
        except Exception:
            pass  # best-effort recovery; don't mask the original error

    def _export_session_if_available(self, session_id: str | None) -> Path | None:
        if session_id is None:
            return None
        export_path = self.paths.external_opencode_dir / f"{session_id}.json"
        try:
            self.opencode_client.export_session(session_id, export_path)
        except JriError:
            return None
        return export_path

    def _create_needs_human_task(
        self,
        original_task: Task,
        *,
        log_path: Path,
        session_id: str | None,
        export_path: Path | None,
    ) -> Task:
        slug = self._allocate_needs_human_slug(original_task.slug)
        task = Task(
            path=self.paths.task_path("todo", slug),
            slug=slug,
            metadata=TaskMetadata(
                title=self._needs_human_title(original_task),
                priority=original_task.metadata.priority,
                assignee="Human",
                depends_on=[],
                acceptance_criteria=[
                    (
                        "Provide the human input or action needed to unblock "
                        f"`{original_task.slug}`."
                    )
                ],
            ),
            body=self._needs_human_body(
                original_task,
                log_path=log_path,
                session_id=session_id,
                export_path=export_path,
            ),
        )
        self._write_task_file(task)
        return task

    def _ensure_promoted_task_pristine(self, task: Task, *, baseline: str) -> None:
        if task.path.read_text(encoding="utf-8") == baseline:
            return
        relative_path = self.git.relative_path(task.path)
        raise JriError(
            f"promoted task file `{relative_path}` was modified in place; "
            "create a follow-up draft task instead"
        )

    def _block_task_on_dependency(self, task: Task, dependency_slug: str) -> Task:
        depends_on = list(task.metadata.depends_on)
        if dependency_slug not in depends_on:
            depends_on.append(dependency_slug)
        return replace(
            task,
            metadata=replace(task.metadata, depends_on=depends_on),
        )

    def _write_task_file(self, task: Task) -> None:
        task.path.parent.mkdir(parents=True, exist_ok=True)
        task.path.write_text(dump_task(task), encoding="utf-8")

    def _allocate_needs_human_slug(self, original_slug: str) -> str:
        base = f"{original_slug}--needs-human"
        used = {
            path.stem
            for status in _TRACKED_TASK_DIRS
            for path in self.paths.task_dir(status).glob("*.md")
        }
        if base not in used:
            return base
        suffix = 2
        while f"{base}-{suffix}" in used:
            suffix += 1
        return f"{base}-{suffix}"

    def _needs_human_title(self, original_task: Task) -> str:
        candidate = f"Unblock {original_task.metadata.title}"
        if len(candidate) <= _MAX_TASK_TITLE_LENGTH:
            return candidate
        return candidate[: _MAX_TASK_TITLE_LENGTH - 3].rstrip() + "..."

    def _needs_human_body(
        self,
        original_task: Task,
        *,
        log_path: Path,
        session_id: str | None,
        export_path: Path | None,
    ) -> str:
        original_path = original_task.path.relative_to(self.root)
        log_relative = log_path.relative_to(self.root)
        export_relative = (
            str(export_path.relative_to(self.root))
            if export_path is not None and export_path.exists()
            else "not available"
        )
        session_label = session_id or "not available"
        return (
            "\n".join(
                (
                    f"Ralph reported `needs human` while working on `{original_path}`.",
                    "",
                    f"Complete this task to unblock `{original_task.slug}`.",
                    "",
                    "## Original Ralph task",
                    f"- Slug: `{original_task.slug}`",
                    f"- Title: {original_task.metadata.title}",
                    f"- Task file: `{original_path}`",
                    "",
                    "## Run artifacts",
                    f"- Ralph log: `{log_relative}`",
                    f"- OpenCode session: `{session_label}`",
                    f"- OpenCode export: `{export_relative}`",
                    "",
                    "## Ralph task description",
                    original_task.body,
                )
            ).rstrip()
            + "\n"
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
