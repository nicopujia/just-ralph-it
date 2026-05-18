import os
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from types import FrameType
from typing import Any, cast

import yaml

from .agents import AgentRuntime, PiRuntime, launch_chat, render_saved_log
from .agents.client import SavedLogRenderer
from .agents.session import detect_latest_session, export_session_if_available, list_sessions, runtime_env
from .errors import HaltRequested, JriError, RestartRequested
from .git import (
    MSG_COMPLETE_HUMAN,
    MSG_ESCALATE_HUMAN,
    MSG_RALPH_FINALIZE,
    MSG_RALPH_INTEGRATE,
    MSG_RECORD_ATTEMPT_HISTORY,
    MSG_RECOVER_FAILED,
    MSG_RECOVER_NEEDS_HUMAN,
    MSG_RECOVER_STALE,
    MSG_START_BEGIN,
    MSG_START_COMPLETE,
    GitRepo,
)
from .graph import GraphCheckResult, check_graph_tree, parse_graph_node_file
from .metrics import MetricEntry, MetricsStore
from .models import (
    ATTEMPT_RESULT_VALUES,
    TASK_STATUSES,
    AgentRunResult,
    AttemptState,
    CompilerTaskSpec,
    HumanTaskPayload,
    ProcessState,
    RalphResultPayload,
    ResetPoint,
    Result,
    RunOutcome,
    RunSummary,
    State,
    Task,
    TaskMetadata,
)
from .paths import JriPaths
from .state import StateStore
from .tasks import create_task_batch, dump_task, list_tasks, move_task, parse_task_file, select_next_task
from .timeline import TimelineEvent, TimelineStore
from .ui import (
    cyan,
    follow_status_bar,
    follow_status_bar_clear,
    supports_color,
    supports_interactive_footer,
    task_footer,
    task_header,
)

_INIT_COMMIT_PATHS = (
    ".jri",
    "Makefile",
    ".jri/learnings.md",
    ".jri/tasks/todo/.gitkeep",
    ".jri/tasks/doing/.gitkeep",
    ".jri/tasks/done/.gitkeep",
    ".jri/graph/.gitkeep",
    ".jri/attempts/.gitkeep",
)
_SCAFFOLD_TEMPLATE_PATHS = (
    ".jri/learnings.md",
    ".jri/tasks/todo/.gitkeep",
    ".jri/tasks/doing/.gitkeep",
    ".jri/tasks/done/.gitkeep",
    ".jri/graph/.gitkeep",
    ".jri/attempts/.gitkeep",
)
_ROOT_SCAFFOLD_PATHS = ("Makefile",)
_TRACKED_TASK_DIRS = TASK_STATUSES
_MAX_TASK_TITLE_LENGTH = 50
_DETACH_NOTICE = "Detached. Use `jri attach` to follow the run again."
_DEFAULT_MAKEFILE = """.PHONY: check

check:
	@echo "make check is not configured yet"
	@false
"""


@dataclass
class _FollowControls:
    enabled: bool
    fd: int | None = None
    stop_requested: bool = False
    confirming_halt: bool = False
    halt_armed: bool = False

    def poll_action(self) -> str | None:
        if not self.enabled or self.fd is None:
            return None
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 0)
        except OSError:
            return None
        if not ready:
            return None
        try:
            key = os.read(self.fd, 1).decode("utf-8", errors="ignore")
        except OSError:
            return None
        return self.handle_key(key.lower())

    def handle_key(self, key: str) -> str | None:
        if not key:
            return None
        if key == "d":
            self._reset_halt_confirmation()
            return "detach"
        if key == "s":
            self._reset_halt_confirmation()
            self.stop_requested = not self.stop_requested
            return "stop" if self.stop_requested else "stop_cancel"
        if self.confirming_halt:
            if key == "n":
                self._reset_halt_confirmation()
                return None
            if key == "y":
                self.halt_armed = True
                return None
            if key in {"\r", "\n"} and self.halt_armed:
                self._reset_halt_confirmation()
                return "halt"
            return None
        if key == "h":
            self.confirming_halt = True
            self.halt_armed = False
        return None

    def _reset_halt_confirmation(self) -> None:
        self.confirming_halt = False
        self.halt_armed = False


class JriService:
    def __init__(self, root: Path, *, agent_runtime: Any | None = None) -> None:
        self.root = root.resolve()
        self.paths = JriPaths(self.root)
        self.git = GitRepo(self.root)
        self.state_store = StateStore(self.paths.state_path)
        self.timeline = TimelineStore(self.paths.timeline_path)
        self.metrics = MetricsStore(self.paths.metrics_path)
        self.agent_runtime: AgentRuntime = cast(AgentRuntime, agent_runtime or PiRuntime())
        self._halt_requested = False
        self._previous_agent_model: str | None = None

    def init(self, *, delete: bool, commit_message: str, branch: str | None = None) -> None:
        repo_exists = self.git.is_repo()
        requested_branch = self.git.validate_default_branch_name(branch) if branch is not None else None
        init_branch = requested_branch or "main"
        self.git.init_if_needed(branch=init_branch)

        if repo_exists and requested_branch is not None:
            self.git.checkout_or_create_branch(requested_branch)

        # Check for existing managed directories
        jri_exists = self.paths.jri_dir.exists()

        if jri_exists:
            if delete:
                # Delete mode: remove existing managed files without prompting
                shutil.rmtree(self.paths.jri_dir)
            else:
                print("Existing .jri/ directory found.")
                print("  [d] Delete - remove existing and reinitialize")
                print("  [a] Abort - cancel initialization")
                print("Choice [d/a]: ", end="", flush=True)

                try:
                    choice = input().strip().lower()
                except EOFError:
                    choice = "a"

                if choice == "d":
                    shutil.rmtree(self.paths.jri_dir)
                else:
                    raise JriError("initialization aborted by user")

        created_files = self._create_scaffold()
        commit_paths: list[str] = list(_INIT_COMMIT_PATHS)
        commit_paths.extend(str(path.relative_to(self.root)) for path in created_files)
        commit_paths = self._commit_paths(commit_paths)
        # Stage all paths first
        self.git.run("add", "-A", "--", *commit_paths)
        # Check if there's anything to commit before committing
        if not self.git.status_short(*commit_paths):
            return
        self.git.run("commit", "-m", commit_message, "--", *commit_paths)

    def chat(
        self, extra_args: list[str], *, fresh: bool = False, model: str | None = None, explore_model: str | None = None
    ) -> int:
        self.ensure_initialized()
        if fresh:
            self.state_store.save_session(None)
        before = {
            session_id
            for session in list_sessions(self.agent_runtime, root=self.root)
            if isinstance((session_id := session.get("id")), str)
        }
        binary = self.agent_runtime.binary if isinstance(self.agent_runtime, PiRuntime) else "pi"
        is_pi_chat_runtime = isinstance(self.agent_runtime, PiRuntime)
        state = self.state_store.load()
        session_id = state.session
        if is_pi_chat_runtime and session_id is not None and session_id not in before:
            self.state_store.save_session(None)
            session_id = None
        session_dir = self.paths.chat_logs_dir if is_pi_chat_runtime else None
        with runtime_env(
            overrides={"interrogator": model, "explore": explore_model}, included_agents={"interrogator", "explorer"}
        ) as env:
            returncode = launch_chat(
                root=self.root,
                session_id=session_id,
                extra_args=extra_args,
                binary=binary,
                env=env,
                session_dir=session_dir,
            )
        if returncode != 0:
            return returncode
        after = list_sessions(self.agent_runtime, root=self.root)
        detected_session_id = detect_latest_session(root=self.root, before=before, sessions=after)
        if detected_session_id is not None:
            session_id = detected_session_id
        if session_id is not None:
            self.state_store.save_session(session_id)
        if not is_pi_chat_runtime:
            export_session_if_available(
                self.agent_runtime,
                root=self.root,
                destination_dir=self.paths.chat_logs_dir,
                timeline=self.timeline,
                session_id=session_id,
            )
        return returncode

    def start(
        self,
        *,
        max_tasks: int | None = None,
        detached: bool = False,
        model: str | None = None,
        validator_model: str | None = None,
        general_model: str | None = None,
        explore_model: str | None = None,
        task_timeout: int | None = None,
        force: bool = False,
        dogfood: bool = False,
    ) -> int:
        self.ensure_initialized()
        self._ensure_not_managed_worktree()
        host_branch = self.git.host_branch()
        self.git.ensure_managed_branches_available(host_branch)
        self._recover_stale_start_state(
            mode="detached" if detached else "foreground", force=force, host_branch=host_branch
        )
        if detached:
            return self._start_detached(
                max_tasks, model, validator_model, general_model, explore_model, task_timeout, dogfood
            )

        return self.run_loop_process(
            max_tasks=max_tasks,
            model=model,
            validator_model=validator_model,
            general_model=general_model,
            explore_model=explore_model,
            task_timeout=task_timeout,
            force=force,
            dogfood=dogfood,
        )

    def run_loop_process(
        self,
        *,
        max_tasks: int | None = None,
        model: str | None = None,
        validator_model: str | None = None,
        general_model: str | None = None,
        explore_model: str | None = None,
        task_timeout: int | None = None,
        force: bool = False,
        recover: bool = False,
        mode: str = "foreground",
        dogfood: bool = False,
    ) -> int:
        self.ensure_initialized()
        self._ensure_not_managed_worktree()
        host_branch = self.git.host_branch()
        self.git.ensure_managed_branches_available(host_branch)
        if recover:
            self._recover_stale_start_state(mode=mode, force=force, host_branch=host_branch)

        if isinstance(self.agent_runtime, PiRuntime):
            return self._run_loop(
                max_tasks,
                task_timeout=task_timeout,
                force=force,
                dogfood=dogfood,
                host_branch=host_branch,
                model_overrides={
                    "ralph": model,
                    "ralph-validator": validator_model,
                    "general": general_model,
                    "explore": explore_model,
                },
            )

        previous_model = self.agent_runtime.model
        self.agent_runtime.model = model
        try:
            return self._run_loop(
                max_tasks, task_timeout=task_timeout, force=force, dogfood=dogfood, host_branch=host_branch
            )
        finally:
            self.agent_runtime.model = previous_model

    def start_summary(
        self,
        *,
        max_tasks: int | None = None,
        model: str | None = None,
        validator_model: str | None = None,
        general_model: str | None = None,
        explore_model: str | None = None,
        task_timeout: int | None = None,
        force: bool = False,
        dogfood: bool = False,
    ) -> RunSummary:
        self.ensure_initialized()
        self._ensure_not_managed_worktree()
        host_branch = self.git.host_branch()
        self.git.ensure_managed_branches_available(host_branch)
        self._recover_stale_start_state(mode="foreground", force=force, host_branch=host_branch)
        if isinstance(self.agent_runtime, PiRuntime):
            return self._run_loop_summary(
                max_tasks,
                task_timeout=task_timeout,
                force=force,
                dogfood=dogfood,
                host_branch=host_branch,
                model_overrides={
                    "ralph": model,
                    "ralph-validator": validator_model,
                    "general": general_model,
                    "explore": explore_model,
                },
            )

        previous_model = self.agent_runtime.model
        self.agent_runtime.model = model
        try:
            return self._run_loop_summary(
                max_tasks, task_timeout=task_timeout, force=force, dogfood=dogfood, host_branch=host_branch
            )
        finally:
            self.agent_runtime.model = previous_model

    def start_attached(
        self,
        *,
        max_tasks: int | None = None,
        model: str | None = None,
        validator_model: str | None = None,
        general_model: str | None = None,
        explore_model: str | None = None,
        task_timeout: int | None = None,
        force: bool = False,
        dogfood: bool = False,
    ) -> int:
        self.ensure_initialized()
        self._ensure_not_managed_worktree()
        host_branch = self.git.host_branch()
        self.git.ensure_managed_branches_available(host_branch)
        self._recover_stale_start_state(mode="foreground", force=force, host_branch=host_branch)
        return self._start_followable(
            max_tasks, model, validator_model, general_model, explore_model, task_timeout, force, dogfood
        )

    def attach(self) -> None:
        self.ensure_initialized()
        state = self.state_store.load()
        process = state.process
        if process is None or not process.log_path:
            raise JriError("no Ralph run is available to attach")
        detached = self._follow_log(Path(process.log_path), loop_pid=process.loop_pid, allow_detach=True)
        if detached:
            self._set_tracked_process_detached(detached=True)

    def inspect(self, slug: str | None = None) -> None:
        self.ensure_initialized()
        attempt = self._resolve_inspect_attempt(slug)
        log_path = self._inspect_log_path(attempt)
        print(task_header(attempt.task_slug))
        rendered = render_saved_log(log_path.read_text(encoding="utf-8"))
        if rendered:
            sys.stdout.write(rendered)
            if not rendered.endswith("\n"):
                sys.stdout.write("\n")
        if attempt.result in ATTEMPT_RESULT_VALUES:
            print(task_footer(attempt.result))
        elif attempt.result is not None:
            print(attempt.result)
        sys.stdout.flush()

    def stop(self, reason: str | None = None) -> None:
        self.ensure_initialized()
        self.paths.signals_dir.mkdir(parents=True, exist_ok=True)
        content = f"{reason}\n" if reason else ""
        self.paths.stop_signal_path.write_text(content, encoding="utf-8")

    def cancel_stop(self) -> None:
        self.ensure_initialized()
        if self.paths.stop_signal_path.exists():
            self.paths.stop_signal_path.unlink()

    def halt(self) -> None:
        self.ensure_initialized()
        if not self._cleanup_tracked_processes(required=True):
            raise JriError("no Ralph process is currently tracked")

    def status(self) -> dict[str, list[Task]]:
        self.ensure_initialized()
        try:
            return {status: list_tasks(self.paths.task_dir(status), git_repo=self.git) for status in _TRACKED_TASK_DIRS}
        except ValueError as exc:
            raise JriError(str(exc)) from exc

    def graph_status(self) -> GraphCheckResult:
        self.ensure_initialized()
        return check_graph_tree(self.root)

    def compile_graph(self) -> dict[str, object]:
        self.ensure_initialized()
        check_result = check_graph_tree(self.root)
        if check_result.errors:
            return {"exit_code": "fail", "errors": list(check_result.errors)}

        changed_graph_paths = self._changed_graph_paths()
        if not changed_graph_paths:
            return {"exit_code": "fail", "errors": ["no uncommitted graph changes to compile"]}

        context = self._compiler_context(changed_graph_paths)
        try:
            raw_result = self._run_intent_compiler(context)
            failure = self._compiler_failure(raw_result)
            if failure is not None:
                return {"exit_code": "fail", "errors": failure}
            specs = self._compiler_task_specs(raw_result)
            tasks = create_task_batch(self.root, specs)
        except (JriError, ValueError, TypeError) as exc:
            return {"exit_code": "fail", "errors": [str(exc)]}

        task_paths = [self.git.relative_path(task.path) for task in tasks]
        commit_paths = self._commit_paths(
            [self._graph_relative_path(path) for path in changed_graph_paths] + task_paths
        )
        try:
            committed = self._commit_compiled_graph("jri: compile graph", commit_paths)
            if not committed:
                raise JriError("no graph or task changes to commit")
        except Exception as exc:
            self._rollback_emitted_tasks(task_paths)
            return {"exit_code": "fail", "errors": [str(exc)]}

        return {
            "exit_code": "success",
            "task_slugs": [task.slug for task in tasks],
            "commit": self.git.rev_parse("HEAD"),
        }

    def _run_intent_compiler(self, context: dict[str, object]) -> dict[str, object]:
        compiler = getattr(self.agent_runtime, "compile_intent_graph", None)
        if compiler is None or not callable(compiler):
            raise JriError("agent runtime does not provide an intent compiler")
        result = compiler(root=self.root, context=context)
        if not isinstance(result, dict):
            raise ValueError("compiler output must be an object")
        return cast(dict[str, object], result)

    def _compiler_context(self, changed_graph_paths: list[str]) -> dict[str, object]:
        return {
            "changed_paths": changed_graph_paths,
            "graph_nodes": [self._compiler_graph_node(path) for path in changed_graph_paths],
            "graph_check": {
                "active_count": check_graph_tree(self.root).active_count,
                "archived_count": check_graph_tree(self.root).archived_count,
                "errors": [],
            },
        }

    def _compiler_graph_node(self, semantic_path: str) -> dict[str, object]:
        node = parse_graph_node_file(self.root, semantic_path)
        payload: dict[str, object] = {
            "path": node.semantic_path,
            "metadata": {"title": node.metadata.title, "state": node.metadata.state},
            "body": node.body,
        }
        if node.metadata.archive_reason is not None:
            cast(dict[str, object], payload["metadata"])["archive_reason"] = node.metadata.archive_reason
        return payload

    def _changed_graph_paths(self) -> list[str]:
        status = self.git.run("status", "--porcelain", "--", ".jri/graph", check=False)
        if status.returncode != 0:
            raise JriError(status.stderr.strip() or "failed to inspect graph changes")
        paths: set[str] = set()
        for line in status.stdout.splitlines():
            if len(line) < 4:
                continue
            raw_path = line[3:].strip()
            if " -> " in raw_path:
                raw_path = raw_path.rsplit(" -> ", 1)[1]
            paths.update(self._semantic_paths_from_graph_status_path(raw_path))
        return sorted(paths)

    def _semantic_paths_from_graph_status_path(self, raw_path: str) -> set[str]:
        graph_prefix = ".jri/graph/"
        if raw_path in {".jri/graph", ".jri/graph/"}:
            return self._all_graph_node_paths()
        if raw_path.endswith("/"):
            base = self.root / raw_path
            return self._node_paths_under(base)
        if not raw_path.startswith(graph_prefix):
            return set()
        if not raw_path.endswith("/NODE.md"):
            return set()
        semantic_path = raw_path.removeprefix(graph_prefix).removesuffix("/NODE.md")
        return {semantic_path} if semantic_path else set()

    def _all_graph_node_paths(self) -> set[str]:
        return self._node_paths_under(self.paths.graph_dir)

    def _node_paths_under(self, base: Path) -> set[str]:
        if not base.exists():
            return set()
        paths: set[str] = set()
        for node_path in base.rglob("NODE.md"):
            try:
                relative = node_path.relative_to(self.paths.graph_dir)
            except ValueError:
                continue
            semantic_path = relative.parent.as_posix()
            if semantic_path and semantic_path != ".":
                paths.add(semantic_path)
        return paths

    def _graph_relative_path(self, semantic_path: str) -> str:
        return f".jri/graph/{semantic_path}/NODE.md"

    def _compiler_failure(self, raw_result: dict[str, object]) -> list[dict[str, object]] | None:
        if raw_result.get("exit_code") != "fail":
            return None
        errors = raw_result.get("errors")
        if not isinstance(errors, list) or not errors:
            raise ValueError("compiler failure must include non-empty `errors`")
        normalized: list[dict[str, object]] = []
        for index, error in enumerate(cast(list[object], errors)):
            if not isinstance(error, dict):
                raise ValueError(f"compiler error[{index}] must be an object")
            item = cast(dict[str, object], error)
            location = item.get("location") or item.get("path")
            ambiguous_area = item.get("ambiguous_area")
            plausible = item.get("plausible_interpretations")
            draft_question = item.get("draft_question")
            if not isinstance(location, str) or not location.strip():
                raise ValueError(f"compiler error[{index}] must include `location`")
            if not isinstance(ambiguous_area, str) or not ambiguous_area.strip():
                raise ValueError(f"compiler error[{index}] must include `ambiguous_area`")
            if (
                not isinstance(plausible, list)
                or not plausible
                or any(
                    not isinstance(candidate, str) or not candidate.strip()
                    for candidate in cast(list[object], plausible)
                )
            ):
                raise ValueError(f"compiler error[{index}] must include `plausible_interpretations`")
            if not isinstance(draft_question, str) or not draft_question.strip():
                raise ValueError(f"compiler error[{index}] must include `draft_question`")
            normalized.append({
                "location": location,
                "ambiguous_area": ambiguous_area,
                "plausible_interpretations": plausible,
                "draft_question": draft_question,
            })
        return normalized

    def _compiler_task_specs(self, raw_result: dict[str, object]) -> list[CompilerTaskSpec]:
        raw_tasks = raw_result.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise ValueError("compiler output must include non-empty `tasks`")
        specs: list[CompilerTaskSpec] = []
        for index, raw_task in enumerate(cast(list[object], raw_tasks)):
            if not isinstance(raw_task, dict):
                raise ValueError(f"task[{index}] must be an object")
            task = cast(dict[str, object], raw_task)
            specs.append(
                CompilerTaskSpec(
                    title=self._compiler_str(task, "title", index),
                    priority=self._compiler_int(task, "priority", index),
                    assignee=cast(Any, self._compiler_str(task, "assignee", index)),
                    depends_on=self._compiler_str_list(task, "depends_on", index),
                    acceptance_criteria=self._compiler_str_list(task, "acceptance_criteria", index),
                    body=self._compiler_str(task, "body", index),
                )
            )
        return specs

    def _compiler_str(self, task: dict[str, object], key: str, index: int) -> str:
        value = task.get(key)
        if not isinstance(value, str):
            raise ValueError(f"task[{index}] `{key}` must be a string")
        return value

    def _compiler_int(self, task: dict[str, object], key: str, index: int) -> int:
        value = task.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"task[{index}] `{key}` must be an integer")
        return value

    def _compiler_str_list(self, task: dict[str, object], key: str, index: int) -> list[str]:
        value = task.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in cast(list[object], value)):
            raise ValueError(f"task[{index}] `{key}` must be a string array")
        return cast(list[str], value)

    def _commit_compiled_graph(self, message: str, paths: list[str]) -> bool:
        return self.git.commit_paths_if_needed(message, paths)

    def _rollback_emitted_tasks(self, task_paths: list[str]) -> None:
        for relative_path in task_paths:
            try:
                (self.root / relative_path).unlink(missing_ok=True)
            except OSError:
                pass
        if task_paths:
            self.git.run("reset", "--", *task_paths, check=False)

    def status_action_needed(self, tasks_by_status: dict[str, list[Task]]) -> str:
        self.ensure_initialized()
        if self._has_stale_runtime_state(tasks_by_status):
            return "run `jri start --force` to recover interrupted Ralph state."

        todo_tasks = tasks_by_status.get("todo", [])
        doing_tasks = tasks_by_status.get("doing", [])
        done_tasks = tasks_by_status.get("done", [])
        done_slugs = {task.slug for task in done_tasks}
        actionable_human_slugs = {
            task.slug
            for status in ("todo", "doing")
            for task in tasks_by_status.get(status, [])
            if task.metadata.assignee == "Human"
        }

        for task in sorted(todo_tasks, key=lambda item: (item.metadata.priority, item.slug)):
            if task.metadata.assignee != "Ralph":
                continue
            blocking_humans = [slug for slug in task.metadata.depends_on if slug in actionable_human_slugs]
            if blocking_humans:
                slug = sorted(blocking_humans)[0]
                return f"complete Human task {slug}, then run `jri complete-human {slug}`."

        try:
            next_task = select_next_task(todo_tasks, done_slugs=done_slugs, doing_tasks=doing_tasks)
        except ValueError:
            next_task = None
        if next_task is not None:
            verb = (
                "retry" if next_task.metadata.depends_on or self._latest_attempt_result(next_task.slug) else "work on"
            )
            return f"run `jri start` to {verb} {next_task.slug}."

        blocked = [
            task
            for task in todo_tasks
            if task.metadata.assignee == "Ralph" and not set(task.metadata.depends_on).issubset(done_slugs)
        ]
        if blocked:
            task = sorted(blocked, key=lambda item: (item.metadata.priority, item.slug))[0]
            missing = sorted(set(task.metadata.depends_on) - done_slugs)
            return f"waiting for dependency {missing[0]} before {task.slug} can start."

        return "none."

    def complete_human(self, slug: str) -> Task:
        self.ensure_initialized()
        task_by_status = self._known_task_by_status(slug)
        if task_by_status is None:
            raise JriError(f"human task not found: {slug}")
        status, task = task_by_status
        if task.metadata.assignee != "Human":
            raise JriError(f"task '{slug}' is assigned to {task.metadata.assignee}, not Human")
        if status == "done":
            raise JriError(f"human task '{slug}' is already done")
        if status not in {"todo", "doing"}:
            raise JriError(f"human task '{slug}' is not actionable from {status}")
        source_path = self.git.relative_path(task.path)
        completed = move_task(task, self.paths.task_dir("done"))
        destination_path = self.git.relative_path(completed.path)
        self.git.commit_paths_if_needed(MSG_COMPLETE_HUMAN.format(slug=slug), [source_path, destination_path])
        self.timeline.record(TimelineEvent(ts=TimelineStore.now_iso(), event="human_task_completed", task=slug))
        return completed

    def ralph_status_summary(self) -> str:
        self.ensure_initialized()
        state = self.state_store.load()
        process = state.process
        loop_pid = process.loop_pid if process is not None else None
        process_alive = loop_pid is not None and self._is_pid_alive(loop_pid)
        active_task = (
            state.active_attempt.task_slug
            if state.active_attempt is not None and state.active_attempt.finished_at is None
            else state.current_task
        )

        if process_alive:
            mode = "detached" if process and process.detached else "attached"
            summary = f"Ralph: running ({mode})"
            if active_task:
                summary += f" on {active_task}"
            if self.paths.stop_signal_path.exists():
                summary += ", stop requested"
            return summary

        if process is not None:
            return "Ralph: not running (previous run was interrupted)"

        try:
            doing_tasks = list_tasks(self.paths.task_dir("doing"), git_repo=self.git)
        except ValueError as exc:
            raise JriError(str(exc)) from exc
        if any(task.metadata.assignee == "Ralph" for task in doing_tasks):
            return "Ralph: not running (task left in doing)"

        if state.active_attempt is not None and state.active_attempt.finished_at is None:
            return "Ralph: not running (previous run was interrupted)"

        return "Ralph: not running"

    def _has_stale_runtime_state(self, tasks_by_status: dict[str, list[Task]]) -> bool:
        state = self.state_store.load()
        process = state.process
        loop_pid = process.loop_pid if process is not None else None
        if loop_pid is not None and self._is_pid_alive(loop_pid):
            return False
        if process is not None:
            return True
        if any(task.metadata.assignee == "Ralph" for task in tasks_by_status.get("doing", [])):
            return True
        return state.active_attempt is not None and state.active_attempt.finished_at is None

    def _latest_attempt_result(self, task_slug: str) -> str | None:
        attempts = [
            attempt
            for attempt in (self.state_store.load().attempts + self._load_attempt_history(task_slug))
            if attempt.task_slug == task_slug and attempt.result is not None
        ]
        if not attempts:
            return None
        return max(attempts, key=lambda attempt: attempt.number).result

    def metrics_summary(self) -> str | None:
        """Return a human-readable metrics summary, or None if no metrics."""
        return self.metrics.summary()

    def reset(self, target_task: str | None = None) -> None:
        """Reset the current host branch to its local runtime reset point."""
        self.ensure_initialized()
        state = self.state_store.load()
        host_branch = self.git.host_branch()
        reset_point = self.resolve_reset_target_point(target_task, host_branch=host_branch)
        target_ref = self._resolve_reset_target_ref(reset_point)

        self._cleanup_tracked_processes(required=False)
        self._ensure_current_host_branch(host_branch)
        self.git.reset_hard(target_ref)
        if self.paths.worktree_dir.exists():
            self.git.remove_worktree(self.paths.worktree_dir)
        ralph_branch = self._ralph_branch(host_branch)
        if self.git.has_local_branch(ralph_branch):
            self.git.delete_branch(ralph_branch)
        self.state_store.save(
            State(
                finished_at=state.finished_at,
                session=state.session,
                branch=state.branch,
                attempts=state.attempts,
                reset_points=state.reset_points,
            )
        )

    def resolve_reset_target_point(
        self, target_task: str | None = None, *, host_branch: str | None = None
    ) -> ResetPoint:
        if host_branch is None:
            host_branch = self.git.host_branch()
        if target_task is not None:
            reset_point = self.state_store.reset_point_for(host_branch=host_branch, task_slug=target_task)
            if reset_point is None:
                raise JriError(f"no reset state found for task '{target_task}' on branch '{host_branch}'")
            return reset_point
        reset_point = self.state_store.latest_reset_point(host_branch=host_branch)
        if reset_point is None:
            raise JriError(f"no reset state found for branch '{host_branch}' - run `jri start` first")
        return reset_point

    def _resolve_reset_target_ref(self, reset_point: ResetPoint) -> str:
        if reset_point.end_commit is not None:
            return self.git.rev_parse(reset_point.end_commit)
        return self.git.rev_parse(reset_point.before_begin_commit)

    def describe_reset_target(self, reset_point: ResetPoint) -> str:
        if reset_point.end_commit is not None:
            return f"completion of {reset_point.task_slug}"
        return f"just before {reset_point.task_slug} began"

    def _describe_reset_target(self, reset_point: ResetPoint) -> str:
        return self.describe_reset_target(reset_point)

    def ensure_initialized(self) -> None:
        self.git.ensure_repo()
        if not self.paths.jri_dir.exists():
            raise JriError("project is not initialized; run `jri init`")

    def _ensure_not_managed_worktree(self) -> None:
        if self.root.name == "worktree" and self.root.parent.name == ".jri":
            raise JriError("jri start cannot run from .jri/worktree; run it from the main repository root")

    def _commit_paths(self, paths: list[str]) -> list[str]:
        scoped_paths: list[str] = []
        for path in dict.fromkeys(paths):
            candidate = self.root / path
            if candidate.exists() or self.git.is_tracked(path):
                scoped_paths.append(path)
        return scoped_paths

    def _list_tasks(self, status: str) -> list[Task]:
        try:
            return list_tasks(self.paths.task_dir(status), git_repo=self.git)
        except ValueError as exc:
            raise JriError(str(exc)) from exc

    def _known_task_by_status(self, slug: str) -> tuple[str, Task] | None:
        for status in _TRACKED_TASK_DIRS:
            for task in self._list_tasks(status):
                if task.slug == slug:
                    return status, task
        return None

    def _lifecycle_task_slugs(self) -> set[str]:
        slugs: set[str] = set()
        for status in ("todo", "doing", "done"):
            slugs.update(task.slug for task in self._list_tasks(status))
        return slugs

    def _lifecycle_task_deps(self) -> dict[str, list[str]]:
        deps: dict[str, list[str]] = {}
        for status in ("todo", "doing", "done"):
            for task in self._list_tasks(status):
                deps[task.slug] = list(task.metadata.depends_on)
        return deps

    def _default_branch(self, host_branch: str | None = None) -> str:
        if host_branch is not None:
            return self.git.validate_default_branch_name(host_branch)
        return self.git.default_branch(hint=self.state_store.load().branch)

    def _ralph_branch(self, host_branch: str) -> str:
        return self.git.ralph_branch_for(host_branch)

    def _managed_ralph_branches(self, host_branch: str | None = None) -> tuple[str, ...]:
        if host_branch is None:
            default = self._default_branch()
            return (f"ralph/{default}", f"ralph-{default}")
        return (self._ralph_branch(host_branch),)

    def has_managed_ralph_branch(self) -> bool:
        try:
            host_branch = self.git.host_branch()
        except JriError:
            return False
        return self.git.has_local_branch(self._ralph_branch(host_branch))

    def _is_managed_ralph_branch(self, branch: str, host_branch: str | None = None) -> bool:
        return branch == "ralph" or branch in self._managed_ralph_branches(host_branch)

    def _ensure_current_host_branch(self, host_branch: str) -> None:
        current = self.git.current_branch()
        if current != host_branch:
            current_description = current or "detached HEAD"
            raise JriError(f"jri runtime branch changed from '{host_branch}' to '{current_description}'")

    def _create_scaffold(self) -> list[Path]:
        created_files: list[Path] = []
        self.paths.jri_dir.mkdir(parents=True, exist_ok=True)
        self.paths.graph_dir.mkdir(parents=True, exist_ok=True)

        self._write_template_files(_SCAFFOLD_TEMPLATE_PATHS)
        created_files.extend(self._write_root_scaffold_files())
        self._write_gitignore_file()
        self.state_store.initialize(branch=self.git.current_branch() or None)
        return created_files

    _GITIGNORE_CONTENT = "logs/\nsignals/\n*state.json*\nmetrics.json\nworktree/\n"

    def _write_gitignore_file(self) -> None:
        self.paths.gitignore_path.write_text(self._GITIGNORE_CONTENT, encoding="utf-8")

    def _write_template_files(self, relative_paths: tuple[str, ...]) -> None:
        for relative_path in relative_paths:
            path = self.root / relative_path
            if path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_load_managed_template(relative_path), encoding="utf-8")

    def _write_root_scaffold_files(self) -> list[Path]:
        created_files: list[Path] = []
        makefile_path = self.root / "Makefile"
        if not makefile_path.exists():
            makefile_path.write_text(_DEFAULT_MAKEFILE, encoding="utf-8")
            created_files.append(makefile_path)

        readme_path = self.paths.readme_path
        if not readme_path.exists():
            readme_path.write_text("", encoding="utf-8")
            created_files.append(readme_path)

        return created_files

    def _start_detached(
        self,
        max_tasks: int | None,
        model: str | None,
        validator_model: str | None,
        general_model: str | None,
        explore_model: str | None,
        task_timeout: int | None,
        dogfood: bool,
    ) -> int:
        state = self.state_store.load()
        if state.process and state.process.loop_pid:
            raise JriError("a Ralph process is already tracked")

        command = [sys.executable, "-m", "jri"]
        if max_tasks is not None:
            command.extend(["-n", str(max_tasks)])
        if model is not None:
            command.extend(["--model", model])
        if validator_model is not None:
            command.extend(["--validator-model", validator_model])
        if general_model is not None:
            command.extend(["--general-model", general_model])
        if explore_model is not None:
            command.extend(["--explore-model", explore_model])
        if task_timeout is not None:
            command.extend(["--task-timeout", str(task_timeout)])
        if dogfood:
            command.append("--dogfood")
        env = os.environ.copy()
        env["JRI_INTERNAL_RUN_LOOP"] = "1"
        if supports_color():
            env["CLICOLOR_FORCE"] = "1"

        log_path = self.paths.ralph_log_path("detached", int(time.time()))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            command, cwd=self.root, env=env, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True
        )
        log_file.close()
        self.state_store.save_process(loop_pid=process.pid, child_pid=None, log_path=log_path, detached=True)
        return 0

    def _start_followable(
        self,
        max_tasks: int | None,
        model: str | None,
        validator_model: str | None,
        general_model: str | None,
        explore_model: str | None,
        task_timeout: int | None,
        force: bool,
        dogfood: bool,
    ) -> int:
        run_log_path = self.paths.ralph_log_path("run", int(time.time()))
        run_log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, "-m", "jri"]
        if max_tasks is not None:
            command.extend(["-n", str(max_tasks)])
        if model is not None:
            command.extend(["--model", model])
        if validator_model is not None:
            command.extend(["--validator-model", validator_model])
        if general_model is not None:
            command.extend(["--general-model", general_model])
        if explore_model is not None:
            command.extend(["--explore-model", explore_model])
        if task_timeout is not None:
            command.extend(["--task-timeout", str(task_timeout)])
        if force:
            command.append("--force")
        if dogfood:
            command.append("--dogfood")
        env = os.environ.copy()
        env["JRI_INTERNAL_RUN_LOOP"] = "1"
        if supports_color():
            env["CLICOLOR_FORCE"] = "1"
        log_file = run_log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            command, cwd=self.root, env=env, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True
        )
        log_file.close()
        self.state_store.save_process(loop_pid=process.pid, child_pid=None, log_path=run_log_path, detached=False)
        detached = self._follow_log(run_log_path, loop_pid=process.pid, loop_process=process, allow_detach=True)
        if detached:
            self._set_tracked_process_detached(detached=True)
            return 0
        return 0 if process.wait() == 0 else 1

    def _run_loop(
        self,
        max_tasks: int | None,
        task_timeout: int | None = None,
        force: bool = False,
        dogfood: bool = False,
        host_branch: str | None = None,
        model_overrides: dict[str, str | None] | None = None,
    ) -> int:
        return self._run_loop_summary(
            max_tasks,
            task_timeout=task_timeout,
            force=force,
            dogfood=dogfood,
            host_branch=host_branch,
            model_overrides=model_overrides,
        ).completed

    def _run_loop_summary(
        self,
        max_tasks: int | None,
        task_timeout: int | None = None,
        force: bool = False,
        dogfood: bool = False,
        host_branch: str | None = None,
        model_overrides: dict[str, str | None] | None = None,
    ) -> RunSummary:
        if host_branch is None:
            host_branch = self.git.host_branch()
            self.git.ensure_managed_branches_available(host_branch)
        try:
            doing = list_tasks(self.paths.task_dir("doing"), git_repo=self.git)
        except ValueError as exc:
            raise JriError(str(exc)) from exc
        if doing:
            raise JriError("a task is already in progress")
        self._handle_dirty_workdir(force=force)
        self._handle_wrong_branch(host_branch=host_branch)
        if self.paths.stop_signal_path.exists():
            self.paths.stop_signal_path.unlink()

        attempted = 0
        completed = 0
        task_results: dict[str, Result] = {}
        outcome: RunOutcome = "no_work"
        failed_slugs: set[str] = set()
        self._halt_requested = False
        old_handlers = self._install_signal_handlers()
        runtime_started_here = False
        runtime_context: AbstractContextManager[dict[str, str]] | None = None
        refresh_runtime = isinstance(self.agent_runtime, PiRuntime) and dogfood
        try:
            if isinstance(self.agent_runtime, PiRuntime) and not refresh_runtime:
                runtime_started_here = True
                runtime_context = self._start_pi_runtime(overrides=model_overrides or {}, host_branch=host_branch)
            while max_tasks is None or attempted < max_tasks:
                if self._halt_requested:
                    raise HaltRequested("Ralph halt requested")

                try:
                    todo_tasks = list_tasks(self.paths.task_dir("todo"), git_repo=self.git)
                    done_tasks = list_tasks(self.paths.task_dir("done"), git_repo=self.git)
                    doing_tasks = list_tasks(self.paths.task_dir("doing"), git_repo=self.git)
                except ValueError as exc:
                    raise JriError(str(exc)) from exc

                def _is_eligible(task: Task) -> bool:
                    return task.slug not in failed_slugs

                next_task = select_next_task(
                    [t for t in todo_tasks if _is_eligible(t)],
                    done_slugs={task.slug for task in done_tasks},
                    doing_tasks=doing_tasks,
                )
                if next_task is None:
                    if not todo_tasks:
                        print("No todo tasks found.")
                    break

                if max_tasks is not None and attempted >= max_tasks:
                    break

                if refresh_runtime:
                    with self._running_pi_runtime(overrides=model_overrides or {}, host_branch=host_branch):
                        result = self._run_task(next_task, host_branch=host_branch, task_timeout=task_timeout)
                else:
                    result = self._run_task(next_task, host_branch=host_branch, task_timeout=task_timeout)
                attempted += 1
                if result == "completed":
                    completed += 1
                    task_results[next_task.slug] = result
                    outcome = "completed"
                    if self._should_restart_process_after_iteration(
                        dogfood=dogfood, max_tasks=max_tasks, completed=attempted
                    ):
                        remaining_tasks = max_tasks - attempted if max_tasks is not None else None
                        raise RestartRequested(remaining_tasks=remaining_tasks)
                elif result in {"failed", "incompleted"}:
                    task_results[next_task.slug] = result
                    outcome = "task_failure"
                    failed_slugs.add(next_task.slug)
                elif result == "needs_human":
                    task_results[next_task.slug] = result
                    outcome = "needs_human"
                    failed_slugs.add(next_task.slug)
                elif result == "timeout":
                    task_results[next_task.slug] = result
                    outcome = "timeout"
                    failed_slugs.add(next_task.slug)
                    self.timeline.record(
                        TimelineEvent(
                            ts=TimelineStore.now_iso(),
                            event="loop_stopped",
                            task=next_task.slug,
                            detail={"reason": "task_timeout", "limit_seconds": task_timeout},
                        )
                    )
                    break

                if self.paths.stop_signal_path.exists():
                    self.paths.stop_signal_path.unlink()
                    break

            # Record if we stopped due to task limit
            if max_tasks is not None and attempted >= max_tasks and outcome != "timeout":
                self.timeline.record(
                    TimelineEvent(
                        ts=TimelineStore.now_iso(),
                        event="loop_stopped",
                        task=None,
                        detail={"reason": "task_limit", "limit": max_tasks},
                    )
                )
        finally:
            if runtime_started_here and isinstance(self.agent_runtime, PiRuntime):
                self._stop_pi_runtime(runtime_context)
            self._restore_signal_handlers(old_handlers)
            self.state_store.clear_process()

        return RunSummary(completed=completed, outcome=outcome, task_results=task_results)

    def _should_restart_process_after_iteration(self, *, dogfood: bool, max_tasks: int | None, completed: int) -> bool:
        if not dogfood:
            return False
        if os.environ.get("JRI_ALLOW_SELF_RESTART") != "1":
            return False
        if self.paths.stop_signal_path.exists():
            return False
        if max_tasks is not None and completed >= max_tasks:
            return False
        return True

    @contextmanager
    def _running_pi_runtime(self, *, overrides: dict[str, str | None], host_branch: str) -> Generator[None]:
        runtime = self._start_pi_runtime(overrides=overrides, host_branch=host_branch)
        try:
            yield
        finally:
            self._stop_pi_runtime(runtime)

    def _start_pi_runtime(
        self, *, overrides: dict[str, str | None], host_branch: str
    ) -> AbstractContextManager[dict[str, str]]:
        if not isinstance(self.agent_runtime, PiRuntime):
            raise JriError("Pi runtime requested for non-Pi agent runtime")
        result_path = self.paths.jri_dir / "signals" / "result"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        # Ensure the worktree exists before Pi starts so Ralph's tools
        # resolve paths against the worktree, not the main repo.
        wt_git, _ = self._ensure_worktree(host_branch)
        self._sync_worktree(wt_git, host_branch=host_branch)
        runtime = runtime_env(overrides=overrides)
        pi_env = runtime.__enter__()
        previous_model = self.agent_runtime.model
        if overrides.get("ralph") is not None:
            self.agent_runtime.model = overrides["ralph"]
        self._previous_agent_model = previous_model
        try:
            self.agent_runtime.start(
                env={**pi_env, "JRI_RESULT_PATH": str(result_path.resolve())}, cwd=self.paths.worktree_dir
            )
        except BaseException as exc:
            self.agent_runtime.model = previous_model
            self._previous_agent_model = None
            runtime.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return runtime

    def _stop_pi_runtime(self, runtime: AbstractContextManager[dict[str, str]] | None) -> None:
        if not isinstance(self.agent_runtime, PiRuntime):
            return
        self.agent_runtime.stop()
        self.agent_runtime.model = self._previous_agent_model
        self._previous_agent_model = None
        if runtime is not None:
            runtime.__exit__(None, None, None)

    def _cleanup_tracked_processes(self, *, required: bool) -> bool:
        state = self.state_store.load()
        process = state.process
        tracked_pids = [] if process is None else [process.child_pid, process.loop_pid]
        has_tracked_process = any(pid is not None for pid in tracked_pids)
        if not has_tracked_process:
            if required:
                return False
            self.state_store.clear_process()
            return False

        current_pid = os.getpid()
        own_pgid = os.getpgrp()
        seen: set[int] = set()
        for pid in tracked_pids:
            if pid is None or pid <= 0 or pid in seen or pid == current_pid:
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
        return True

    def _ensure_worktree(self, host_branch: str) -> tuple[GitRepo, JriPaths]:
        """Ensure the persistent Ralph worktree exists and return helpers."""
        wt_dir = self.paths.worktree_dir
        branch = self._ralph_branch(host_branch)

        if not self.git.has_local_branch(branch):
            host_ref = self.git.rev_parse(host_branch)
            result = self.git.run("branch", branch, host_ref, check=False)
            if result.returncode != 0:
                raise JriError(result.stderr.strip() or f"failed to create branch {branch}")

        if not wt_dir.exists():
            self.git.prune_worktrees()
            self.git.add_worktree(wt_dir, branch)

        return GitRepo(wt_dir), JriPaths(wt_dir)

    def _sync_worktree(self, wt_git: GitRepo, *, host_branch: str) -> None:
        """Reset Ralph's worktree branch to the host-branch tip."""
        branch = self._ralph_branch(host_branch)
        active_attempt = self.state_store.load().active_attempt
        if (
            active_attempt is not None
            and active_attempt.branch == branch
            and self._is_completed_attempt_payload(active_attempt)
            and self._attempt_has_unintegrated_branch_work(active_attempt)
        ):
            wt_git.run("checkout", "--force", branch)
            wt_git.run("clean", "-fd")
            return
        host_ref = self.git.rev_parse(host_branch)
        self.git.reset_branch(branch, host_ref)
        wt_git.run("checkout", "--force", branch)
        wt_git.run("clean", "-fd")

    def _is_completed_attempt_payload(self, attempt: AttemptState) -> bool:
        return attempt.result == "completed" or (
            attempt.result_payload is not None and attempt.result_payload.result == "completed"
        )

    def _result_payload_violation(self, result: AgentRunResult) -> str | None:
        if result.result not in {"completed", "incompleted", "needs_human"}:
            return None
        if result.payload is None:
            return "missing_result_payload"
        if result.payload.result != result.result:
            return "result_payload_mismatch"
        return None

    def _attempt_has_unintegrated_branch_work(self, attempt: AttemptState) -> bool:
        host_branch = self._host_branch_for_managed_attempt(attempt.branch)
        if host_branch is None:
            return False
        if not self.git.has_local_branch(attempt.branch):
            return False
        return not self.git.is_ancestor(attempt.branch, host_branch)

    def _host_branch_for_managed_attempt(self, branch: str) -> str | None:
        prefix = "ralph/"
        if not branch.startswith(prefix):
            return None
        host_branch = branch.removeprefix(prefix)
        if not host_branch:
            return None
        self.git.validate_default_branch_name(host_branch)
        return host_branch

    def _validate_attempt_host_branch(self, attempt: AttemptState, *, host_branch: str) -> str:
        attempt_host_branch = self._host_branch_for_managed_attempt(attempt.branch)
        if attempt_host_branch is None:
            raise JriError(f'active attempt branch "{attempt.branch}" is not a managed Ralph branch')
        if attempt_host_branch != host_branch:
            raise JriError(
                "active attempt belongs to host branch "
                f'"{attempt_host_branch}", not current host branch "{host_branch}"'
            )
        return attempt_host_branch

    def _integrate_completed_branch(self, *, task_slug: str, branch: str, host_branch: str | None = None) -> None:
        if host_branch is None:
            host_branch = self.git.host_branch()
            self.git.ensure_managed_branches_available(host_branch)
        if not self.git.has_local_branch(branch):
            return
        if self.git.is_ancestor(branch, host_branch):
            return
        self._ensure_current_host_branch(host_branch)
        if self.git.status_short():
            raise JriError("git working tree must be clean before integrating Ralph work")
        if self.git.is_ancestor(host_branch, branch):
            self.git.merge_ff_only(branch)
            return
        self.git.merge_no_ff(branch, message=MSG_RALPH_INTEGRATE.format(slug=task_slug))

    def _previous_attempts_prompt_section(self, task_slug: str) -> str:
        attempts = [
            attempt
            for attempt in self._load_attempt_history(task_slug)
            if attempt.result in {"incompleted", "needs_human", "failed", "timeout"}
        ]
        if not attempts:
            return ""

        history_path = self.paths.attempt_history_path(task_slug).relative_to(self.paths.root)
        return (
            f"Previous retry-relevant attempts: {len(attempts)}.\n"
            f"History file: {history_path.as_posix()}.\n"
            f"Inspect it with `jri inspect {task_slug}` before retrying."
        )

    def _run_task(self, task: Task, *, host_branch: str, task_timeout: int | None = None) -> Result:
        state = self.state_store.load()
        started_at = int(time.time())
        log_path = self.paths.ralph_log_path(task.slug, started_at)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        branch = self._ralph_branch(host_branch)
        print(task_header(task.slug))
        sys.stdout.flush()

        # Calculate deadline if task_timeout is set
        deadline: int | None = None
        if task_timeout is not None and task_timeout > 0:
            deadline = started_at + task_timeout

        wt_git, wt_paths = self._ensure_worktree(host_branch)

        attempt = AttemptState(
            number=len(state.attempts) + 1,
            task_slug=task.slug,
            branch=branch,
            started_at=started_at,
            log_path=str(log_path),
        )
        self.state_store.start_attempt(attempt)
        self.state_store.mark_task_started(task_slug=task.slug, started_at=started_at)
        self.timeline.record(
            TimelineEvent(
                ts=TimelineStore.now_iso(),
                event="attempt_started",
                task=task.slug,
                detail={"attempt": attempt.number, "branch": branch, "log_path": str(log_path)},
            )
        )
        before_begin_commit = self.git.rev_parse(host_branch)
        main_doing_task = move_task(task, self.paths.task_dir("doing"))
        self.git.commit_all_if_needed(MSG_START_BEGIN.format(slug=task.slug))
        begin_commit = self.git.rev_parse(host_branch)
        self.state_store.save_reset_point(
            ResetPoint(
                task_slug=task.slug,
                host_branch=host_branch,
                ralph_branch=branch,
                before_begin_commit=before_begin_commit,
                begin_commit=begin_commit,
                started_at=started_at,
            )
        )
        self._sync_worktree(wt_git, host_branch=host_branch)
        # Now read the same task from the worktree where Ralph will work.
        wt_task_path = wt_paths.task_path("doing", task.slug)
        doing_task = parse_task_file(wt_task_path)
        doing_task_baseline = doing_task.path.read_text(encoding="utf-8")
        # Keep a reference to the main repo's path for later cleanup.
        del main_doing_task
        self._save_runtime_process(child_pid=None, task_log_path=log_path)

        previous_attempts = self._previous_attempts_prompt_section(task.slug)
        prompt_text = (
            f"Solve `{doing_task.path.relative_to(wt_paths.root)}`. Commit frequently. "
            "Stay on the Ralph worktree/branch; the runtime handles integration, "
            "so do not "
            "merge to the default branch yourself."
        )
        if previous_attempts:
            prompt_text = f"{prompt_text}\n\n{previous_attempts}"

        def on_start_cb(child_pid: int) -> None:
            self._save_runtime_process(child_pid=child_pid, task_log_path=log_path)

        result_path = self.paths.jri_dir / "signals" / "result"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = self.agent_runtime.run_ralph_task(
                root=wt_paths.root,
                prompt=prompt_text,
                log_path=log_path,
                result_path=result_path,
                on_start=on_start_cb,
                timeout=task_timeout,
            )
        except Exception as exc:
            message = f"JRI failed to run Ralph task: {type(exc).__name__}: {exc}"
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(f"{message}\n")
            print(message, file=sys.stderr)
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(), event="stderr_warning", task=task.slug, detail={"message": message}
                )
            )
            self._recover_failed_task_wt(doing_task, wt_git, host_branch=host_branch)
            self._finish_attempt(attempt, result="failed")
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="task_failed",
                    task=task.slug,
                    detail={"reason": "agent_runtime_exception", "error_type": type(exc).__name__, "error": str(exc)},
                )
            )
            print(task_footer("failed"))
            sys.stdout.flush()
            return "failed"

        attempt = replace(attempt, session_id=result.session_id, result_payload=result.payload)
        self.state_store.save_active_attempt(attempt)

        # Check for task timeout
        finished_at = int(time.time())
        if result.result == "timeout" or (deadline is not None and finished_at > deadline):
            timeout_msg = f"Task {task.slug} exceeded timeout of {task_timeout}s (took {finished_at - started_at}s)"
            print(timeout_msg, file=sys.stderr)
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(), event="stderr_warning", task=task.slug, detail={"message": timeout_msg}
                )
            )
            self._recover_failed_task_wt(doing_task, wt_git, host_branch=host_branch)
            self._finish_attempt(attempt, result="timeout")
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="task_failed",
                    task=task.slug,
                    detail={"reason": "task_timeout", "limit_seconds": task_timeout},
                )
            )
            print(task_footer("timeout"))
            sys.stdout.flush()
            return "timeout"

        # Record any warnings from the Pi run (e.g., missing result payload)
        for warning in result.warnings:
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(), event="stderr_warning", task=task.slug, detail={"message": warning}
                )
            )

        payload_violation = self._result_payload_violation(result)
        if payload_violation is not None:
            self._recover_failed_task_wt(doing_task, wt_git, host_branch=host_branch)
            self._finish_attempt(attempt, result="failed")
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="task_failed",
                    task=task.slug,
                    detail={"reason": payload_violation},
                )
            )
            print(task_footer("failed"))
            sys.stdout.flush()
            return "failed"

        if result.returncode != 0:
            self._recover_failed_task_wt(doing_task, wt_git, host_branch=host_branch)
            self._finish_attempt(attempt, result="failed")
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="task_failed",
                    task=task.slug,
                    detail={"reason": "nonzero_returncode", "returncode": result.returncode},
                )
            )
            print(task_footer("failed"))
            sys.stdout.flush()
            return "failed"

        export_path = export_session_if_available(
            self.agent_runtime,
            root=self.root,
            destination_dir=self.paths.external_pi_dir,
            timeline=self.timeline,
            session_id=result.session_id,
            task_slug=task.slug,
        )
        if not doing_task.path.exists():
            self._recover_failed_task_wt(doing_task, wt_git, host_branch=host_branch)
            self._finish_attempt(attempt, result="failed")
            relative_path = doing_task.path.relative_to(wt_paths.root)
            raise JriError(f"task file `{relative_path}` disappeared during Ralph run")

        # If a project tool (like prettier) modified the task file in
        # place, restore it to baseline rather than failing the whole
        # task. Ralph's actual work is still valid.
        if doing_task.path.read_text(encoding="utf-8") != doing_task_baseline:
            doing_task.path.write_text(doing_task_baseline, encoding="utf-8")

        if result.result == "needs_human":
            self._recover_needs_human_task(
                doing_task,
                result.payload,
                log_path=log_path,
                session_id=result.session_id,
                export_path=export_path,
                host_branch=host_branch,
            )
            self._finish_attempt(attempt, result="needs_human")
            self.timeline.record(TimelineEvent(ts=TimelineStore.now_iso(), event="task_needs_human", task=task.slug))
            print(task_footer("needs_human"))
            sys.stdout.flush()
            return "needs_human"

        if result.result == "incompleted" and (result.payload is None or not result.payload.learnings):
            self._recover_failed_task_wt(doing_task, wt_git, host_branch=host_branch)
            self._finish_attempt(attempt, result="failed")
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="task_failed",
                    task=task.slug,
                    detail={"reason": "incompleted_missing_learnings"},
                )
            )
            print(task_footer("failed"))
            sys.stdout.flush()
            return "failed"

        if result.result in {"failed", "incompleted"}:
            self._recover_failed_task_wt(doing_task, wt_git, host_branch=host_branch)
            self._finish_attempt(attempt, result=result.result)
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="task_failed",
                    task=task.slug,
                    detail={"reason": f"ralph_result_{result.result}"},
                )
            )
            print(task_footer(result.result))
            sys.stdout.flush()
            return result.result

        wt_git.commit_all_if_needed(MSG_RALPH_FINALIZE.format(slug=task.slug))

        if (wt_paths.root / "Makefile").exists():
            try:
                check = subprocess.run(["make", "check"], cwd=wt_paths.root, capture_output=True, text=True)
            except FileNotFoundError:
                make_msg = "make: command not found"
                print(make_msg, file=sys.stderr)
                self.timeline.record(
                    TimelineEvent(
                        ts=TimelineStore.now_iso(), event="stderr_warning", task=task.slug, detail={"message": make_msg}
                    )
                )
                self._recover_failed_task_wt(doing_task, wt_git, host_branch=host_branch)
                self.metrics.record(MetricEntry(task=task.slug, ts=MetricsStore.now_iso(), result="fail"))
                self._finish_attempt(attempt, result="failed")
                print(task_footer("failed"))
                sys.stdout.flush()
                return "failed"
            if check.returncode != 0:
                make_fail_msg = f"make check failed for {task.slug}:\n{check.stderr}"
                print(make_fail_msg, file=sys.stderr)
                self.timeline.record(
                    TimelineEvent(
                        ts=TimelineStore.now_iso(),
                        event="stderr_warning",
                        task=task.slug,
                        detail={"message": make_fail_msg[:500] if make_fail_msg else ""},
                    )
                )
                self.timeline.record(
                    TimelineEvent(
                        ts=TimelineStore.now_iso(),
                        event="make_check_failed",
                        task=task.slug,
                        detail={"stderr": check.stderr[:500] if check.stderr else ""},
                    )
                )
                self._recover_failed_task_wt(doing_task, wt_git, host_branch=host_branch)
                self.metrics.record(MetricEntry(task=task.slug, ts=MetricsStore.now_iso(), result="fail"))
                self._finish_attempt(attempt, result="failed")
                print(task_footer("failed"))
                sys.stdout.flush()
                return "failed"
            self.timeline.record(TimelineEvent(ts=TimelineStore.now_iso(), event="make_check_passed", task=task.slug))
            self.metrics.record(MetricEntry(task=task.slug, ts=MetricsStore.now_iso(), result="pass"))

        finished_at = int(time.time())
        attempt = replace(attempt, finished_at=finished_at, result="completed")
        self.state_store.save_active_attempt(attempt)

        self._integrate_completed_branch(task_slug=task.slug, branch=branch, host_branch=host_branch)

        if not (self.paths.task_path("doing", task.slug)).exists():
            relative_path = f".jri/tasks/doing/{task.slug}.md"
            raise JriError(f"task file `{relative_path}` disappeared during Ralph run")
        doing_on_main = parse_task_file(self.paths.task_path("doing", task.slug))
        move_task(doing_on_main, self.paths.task_dir("done"))
        self.git.commit_all_if_needed(MSG_START_COMPLETE.format(slug=task.slug))
        end_commit = self.git.rev_parse(host_branch)
        reset_point = self.state_store.reset_point_for(host_branch=host_branch, task_slug=task.slug)
        if reset_point is not None:
            self.state_store.save_reset_point(replace(reset_point, end_commit=end_commit, finished_at=finished_at))
        self._save_diff_artifact(task.slug)

        if self.git.has_remote():
            self.git.push_task_refs(branch=branch, host_branch=host_branch)

        self.state_store.save_active_attempt(attempt)
        self._persist_attempt_history(attempt)
        self.state_store.mark_task_finished(task_slug=task.slug, finished_at=finished_at)
        self.state_store.clear_active_attempt()
        self.timeline.record(TimelineEvent(ts=TimelineStore.now_iso(), event="task_completed", task=task.slug))
        print(task_footer("completed"))
        sys.stdout.flush()
        return "completed"

    def _recover_failed_task_wt(self, doing_task: Task, wt_git: GitRepo, *, host_branch: str) -> None:
        """Recover from a failed task in the worktree."""
        try:
            # Move task back to todo on main repo first.
            main_doing = self.paths.task_path("doing", doing_task.slug)
            if main_doing.exists():
                main_task = parse_task_file(main_doing)
                move_task(main_task, self.paths.task_dir("todo"))
                self.git.commit_all_if_needed(MSG_RECOVER_FAILED.format(slug=doing_task.slug))
            # Reset the worktree so it reflects main's new state.
            self._sync_worktree(wt_git, host_branch=host_branch)
            self._reset_runtime_state()
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="recovery_completed",
                    task=doing_task.slug,
                    detail={"reason": "task_failed"},
                )
            )
        except Exception as recovery_error:
            self._record_recovery_failure(task_slug=doing_task.slug, phase="recover-failed-task", error=recovery_error)
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="cleanup_failed",
                    task=doing_task.slug,
                    detail={
                        "phase": "recover-failed-task",
                        "error_type": type(recovery_error).__name__,
                        "error": str(recovery_error),
                    },
                )
            )

    def _handle_dirty_workdir(self, *, force: bool) -> None:
        """Handle uncommitted changes before starting the loop."""
        status = self.git.status_short()
        if not status:
            return
        if force:
            self.git.run("stash")
            return
        sys.stdout.write("Uncommitted changes detected:\n")
        for line in status.splitlines():
            sys.stdout.write(f"  {line}\n")
        sys.stdout.write("\n")
        sys.stdout.write("  [s] Stash and continue\n")
        sys.stdout.write("  [d] Discard and continue\n")
        sys.stdout.write("  [a] Abort\n")
        sys.stdout.write("Choice [s/d/a]: ")
        sys.stdout.flush()
        choice = input().strip().lower()
        if choice == "s":
            self.git.run("stash")
        elif choice == "d":
            self.git.run("checkout", ".")
            self.git.run("clean", "-fd")
        else:
            raise JriError("aborted by user")

    def _status_paths(self, status: str) -> list[str]:
        paths: list[str] = []
        for line in status.splitlines():
            if len(line) < 4:
                continue
            path = line[3:]
            if " -> " in path:
                path = path.rsplit(" -> ", 1)[-1]
            paths.append(path)
        return paths

    def _handle_wrong_branch(self, *, host_branch: str) -> None:
        """Ensure the runtime is still on the captured host branch."""
        self._ensure_current_host_branch(host_branch)

    def _recover_stale_start_state(self, *, mode: str, force: bool = False, host_branch: str | None = None) -> None:
        if host_branch is None:
            host_branch = self.git.host_branch()
            self.git.ensure_managed_branches_available(host_branch)
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
            raise JriError("a Ralph process is already running; use `jri attach` to follow it")

        if doing_tasks:
            if not force:
                slug = doing_tasks[0].slug
                sys.stdout.write(f'Task "{slug}" has incomplete work from a crashed run.\n')
                sys.stdout.write("Reset and move back to todo? [Y/n] ")
                sys.stdout.flush()
                choice = input().strip().lower()
                if choice not in ("", "y"):
                    raise JriError("aborted by user")
            reason = "dead-tracked-process" if loop_pid is not None else "no-tracked-process"
            active_attempt = state.active_attempt
            if active_attempt is not None:
                if not self._attempt_matches_task(active_attempt, doing_tasks[0]):
                    raise JriError("active attempt does not match the task in progress")
                completion_evidence = self._attempt_completion_evidence(active_attempt)
                if completion_evidence is not None:
                    self._validate_attempt_host_branch(active_attempt, host_branch=host_branch)
                    self._record_recovery(
                        mode=mode,
                        reason="resume-completed-attempt",
                        task_slug=doing_tasks[0].slug,
                        process=process,
                        evidence=completion_evidence,
                    )
                    self._complete_attempt(active_attempt, doing_task=doing_tasks[0], host_branch=host_branch)
                    return
                if active_attempt.result == "completed":
                    reason = "missing-completion-evidence"
            self._recover_stale_task(doing_tasks[0], mode=mode, reason=reason, process=process, host_branch=host_branch)
            return

        if state.active_attempt is not None:
            active_attempt = state.active_attempt
            completion_evidence = self._attempt_completion_evidence(active_attempt)
            if completion_evidence is not None:
                self._validate_attempt_host_branch(active_attempt, host_branch=host_branch)
                self._record_recovery(
                    mode=mode,
                    reason="resume-completed-attempt",
                    task_slug=active_attempt.task_slug,
                    process=process,
                    evidence=completion_evidence,
                )
                self._complete_attempt(active_attempt, doing_task=None, host_branch=host_branch)
                return
            task_status = self._tracked_task_status(active_attempt.task_slug)
            if task_status in {"doing", "done"}:
                self._validate_attempt_host_branch(active_attempt, host_branch=host_branch)
                self._recover_unverified_completed_attempt(
                    active_attempt,
                    mode=mode,
                    reason="missing-completion-evidence",
                    process=process,
                    host_branch=host_branch,
                )
                return
            if active_attempt.result in {"failed", "incompleted", "needs_human", "interrupted"}:
                self._reset_runtime_state()
                self.state_store.clear_active_attempt()
                return

        if process is not None:
            reason = "dead-tracked-process" if loop_pid is not None else "missing-loop-pid"
            self._record_recovery(mode=mode, reason=reason, task_slug=None, process=process)
            self._mark_active_attempt_interrupted()
            self._reset_runtime_state()
            return

        if state.started_at is not None:
            self._record_recovery(mode=mode, reason="stale-task-state", task_slug=None, process=None)
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

    def _recover_stale_task(
        self, doing_task: Task, *, mode: str, reason: str, process: ProcessState | None, host_branch: str
    ) -> None:
        try:
            self._ensure_current_host_branch(host_branch)
            if self.git.status_short():
                raise JriError("git working tree must be clean before stale recovery")

            # Reset worktree if it exists
            if self.paths.worktree_dir.exists():
                wt_git = GitRepo(self.paths.worktree_dir)
                self._sync_worktree(wt_git, host_branch=host_branch)

            move_task(doing_task, self.paths.task_dir("todo"))
            self._record_recovery(mode=mode, reason=reason, task_slug=doing_task.slug, process=process)
            self._mark_active_attempt_interrupted()
            self._reset_runtime_state()
            self.git.commit_all_if_needed(MSG_RECOVER_STALE.format(slug=doing_task.slug))
        except Exception as recovery_error:
            self._record_recovery_failure(task_slug=doing_task.slug, phase="recover-stale-task", error=recovery_error)
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="cleanup_failed",
                    task=doing_task.slug,
                    detail={
                        "phase": "recover-stale-task",
                        "error_type": type(recovery_error).__name__,
                        "error": str(recovery_error),
                    },
                )
            )
            raise

    def _reset_runtime_state(self) -> None:
        state = self.state_store.load()
        self.state_store.save(
            State(
                finished_at=state.finished_at,
                session=state.session,
                branch=state.branch,
                active_attempt=state.active_attempt,
                attempts=state.attempts,
                reset_points=state.reset_points,
            )
        )

    def _finish_attempt(self, attempt: AttemptState, *, result: str) -> None:
        finished_at = int(time.time())
        attempt = replace(attempt, finished_at=finished_at, result=result)
        self.state_store.save_active_attempt(attempt)
        self._persist_attempt_history(attempt)
        self.state_store.clear_active_attempt()

    def _mark_active_attempt_interrupted(self) -> None:
        state = self.state_store.load()
        if state.active_attempt is None:
            return
        attempt = replace(
            state.active_attempt, finished_at=state.active_attempt.finished_at or int(time.time()), result="interrupted"
        )
        self.state_store.save_active_attempt(attempt)
        self._persist_attempt_history(attempt)
        self.state_store.clear_active_attempt()

    def _attempt_matches_task(self, attempt: AttemptState, task: Task) -> bool:
        branch_ok = self._host_branch_for_managed_attempt(attempt.branch) is not None
        return attempt.task_slug == task.slug and branch_ok

    def _attempt_completion_evidence(self, attempt: AttemptState) -> dict[str, str] | None:
        evidence: dict[str, str] = {}
        history_entry = next(
            (
                entry
                for entry in self._load_attempt_history(attempt.task_slug)
                if entry.number == attempt.number and entry.result == "completed" and entry.finished_at is not None
            ),
            None,
        )
        if history_entry is not None:
            evidence["attempt_history"] = (
                f"{self.git.relative_path(self.paths.attempt_history_path(attempt.task_slug))}#{attempt.number}"
            )

        if (self.root / "Makefile").exists():
            make_check_passed_ts = self._timeline_event_ts(
                task_slug=attempt.task_slug, event="make_check_passed", not_before=attempt.started_at
            )
            if make_check_passed_ts is not None:
                evidence["make_check_passed_event"] = make_check_passed_ts

        host_branch = self._host_branch_for_managed_attempt(attempt.branch)
        if host_branch is not None:
            reset_point = self.state_store.reset_point_for(host_branch=host_branch, task_slug=attempt.task_slug)
            if reset_point is not None and reset_point.end_commit is not None:
                evidence["reset_point"] = reset_point.end_commit

        if self.paths.task_path("done", attempt.task_slug).exists():
            evidence["task_status"] = "done"

        if self._is_completed_attempt_payload(attempt) and self._attempt_has_unintegrated_branch_work(attempt):
            evidence["branch_work"] = attempt.branch

        if "attempt_history" in evidence and ("make_check_passed_event" in evidence or "reset_point" in evidence):
            return evidence
        if "branch_work" in evidence and self._is_completed_attempt_payload(attempt):
            return evidence
        if {"task_status", "reset_point", "make_check_passed_event"}.issubset(evidence):
            return evidence
        return None

    def _timeline_event_ts(self, *, task_slug: str, event: str, not_before: int | None) -> str | None:
        minimum_ts = (
            datetime.fromtimestamp(not_before, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if not_before is not None else None
        )
        for timeline_event in reversed(self.timeline.read()):
            if timeline_event.task != task_slug or timeline_event.event != event:
                continue
            if minimum_ts is not None and timeline_event.ts < minimum_ts:
                continue
            return timeline_event.ts
        return None

    def _tracked_task_status(self, slug: str) -> str | None:
        for status in _TRACKED_TASK_DIRS:
            if self.paths.task_path(status, slug).exists():
                return status
        return None

    def _recover_unverified_completed_attempt(
        self, attempt: AttemptState, *, mode: str, reason: str, process: ProcessState | None, host_branch: str
    ) -> None:
        self._validate_attempt_host_branch(attempt, host_branch=host_branch)
        try:
            self._ensure_current_host_branch(host_branch)
            if self.git.status_short():
                raise JriError("git working tree must be clean before stale recovery")

            if self.paths.worktree_dir.exists():
                wt_git = GitRepo(self.paths.worktree_dir)
                self._sync_worktree(wt_git, host_branch=host_branch)

            task_moved = False
            for status in ("doing", "done"):
                task_path = self.paths.task_path(status, attempt.task_slug)
                if not task_path.exists():
                    continue
                move_task(parse_task_file(task_path), self.paths.task_dir("todo"))
                task_moved = True
                break

            self._record_recovery(mode=mode, reason=reason, task_slug=attempt.task_slug, process=process)
            self._mark_active_attempt_interrupted()
            self._reset_runtime_state()
            if task_moved:
                self.git.commit_all_if_needed(MSG_RECOVER_STALE.format(slug=attempt.task_slug))
        except Exception as recovery_error:
            self._record_recovery_failure(
                task_slug=attempt.task_slug, phase="recover-unverified-completed-attempt", error=recovery_error
            )
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="cleanup_failed",
                    task=attempt.task_slug,
                    detail={
                        "phase": "recover-unverified-completed-attempt",
                        "error_type": type(recovery_error).__name__,
                        "error": str(recovery_error),
                    },
                )
            )
            raise

    def _complete_attempt(
        self, attempt: AttemptState, *, doing_task: Task | None, host_branch: str | None = None
    ) -> None:
        if host_branch is None:
            host_branch = self.git.host_branch()
            self.git.ensure_managed_branches_available(host_branch)
        self._validate_attempt_host_branch(attempt, host_branch=host_branch)
        self._ensure_current_host_branch(host_branch)
        if self.git.status_short():
            raise JriError("git working tree must be clean before completing Ralph work")

        self._integrate_completed_branch(task_slug=attempt.task_slug, branch=attempt.branch, host_branch=host_branch)

        if doing_task is not None and doing_task.path.exists():
            move_task(doing_task, self.paths.task_dir("done"))
        self.git.commit_all_if_needed(MSG_START_COMPLETE.format(slug=attempt.task_slug))
        finished_at = attempt.finished_at or int(time.time())
        end_commit = self.git.rev_parse(host_branch)
        reset_point = self.state_store.reset_point_for(host_branch=host_branch, task_slug=attempt.task_slug)
        if reset_point is not None:
            self.state_store.save_reset_point(replace(reset_point, end_commit=end_commit, finished_at=finished_at))
        self._save_diff_artifact(attempt.task_slug)
        if self.git.has_remote() and self.git.has_local_branch(attempt.branch):
            self.git.push_task_refs(branch=attempt.branch, host_branch=host_branch)

        attempt = replace(attempt, finished_at=finished_at, result="completed")
        self.state_store.save_active_attempt(attempt)
        self._persist_attempt_history(attempt)
        self.state_store.mark_task_finished(task_slug=attempt.task_slug, finished_at=finished_at)
        self.state_store.clear_process()
        self.state_store.clear_active_attempt()

    def _record_recovery(
        self,
        *,
        mode: str,
        reason: str,
        task_slug: str | None,
        process: ProcessState | None,
        evidence: dict[str, str] | None = None,
    ) -> None:
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        loop_pid = process.loop_pid if process is not None else None
        child_pid = process.child_pid if process is not None else None
        detached = process.detached if process is not None else False
        log_path = process.log_path if process is not None else None
        parts = [
            timestamp,
            "event=stale-run-recovery",
            f"mode={mode}",
            f"task={task_slug or '-'}",
            f"reason={reason}",
            f"loop_pid={loop_pid if loop_pid is not None else '-'}",
            f"child_pid={child_pid if child_pid is not None else '-'}",
            f"detached={'true' if detached else 'false'}",
            f"log_path={log_path or '-'}",
        ]
        if evidence:
            evidence_summary = ",".join(f"{key}:{value}" for key, value in sorted(evidence.items()))
            parts.append(f"evidence={evidence_summary}")
        line = " ".join(parts)
        self.paths.recovery_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.recovery_log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        self.timeline.record(
            TimelineEvent(
                ts=TimelineStore.now_iso(),
                event="recovery_completed",
                task=task_slug,
                detail={
                    "mode": mode,
                    "reason": reason,
                    "message": line,
                    **({"evidence": evidence} if evidence else {}),
                },
            )
        )

    def _record_recovery_failure(self, *, task_slug: str | None, phase: str, error: Exception) -> None:
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        error_type = type(error).__name__
        error_message = str(error).replace("\n", "\\n")
        line = " ".join((
            timestamp,
            "event=recovery-failure",
            f"task={task_slug or '-'}",
            f"phase={phase}",
            f"error_type={error_type}",
            f"error_message={error_message}",
        ))
        self.paths.recovery_failures_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.recovery_failures_log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _persist_attempt_history(self, attempt: AttemptState) -> None:
        history_path = self.paths.attempt_history_path(attempt.task_slug)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        if history_path.exists():
            payload: object = yaml.safe_load(history_path.read_text(encoding="utf-8"))
            attempts = cast(dict[str, object], payload).get("attempts") if isinstance(payload, dict) else None
            history: list[dict[str, object]] = (
                [cast(dict[str, object], item) for item in cast(list[object], attempts) if isinstance(item, dict)]
                if isinstance(attempts, list)
                else []
            )
        else:
            history = []
        history = [{key: value for key, value in existing.items() if key != "task_slug"} for existing in history]
        serialized = {key: value for key, value in attempt.to_payload().items() if key != "task_slug"}
        updated = False
        for index, existing in enumerate(history):
            if existing.get("number") == attempt.number:
                history[index] = serialized
                updated = True
                break
        if not updated:
            history.append(serialized)
        payload = yaml.safe_dump(
            {"attempts": history}, sort_keys=True, default_flow_style=False, allow_unicode=False
        ).rstrip("\n")
        history_path.write_text(payload + "\n", encoding="utf-8")
        self.git.commit_paths_if_needed(
            MSG_RECORD_ATTEMPT_HISTORY.format(slug=attempt.task_slug), [self.git.relative_path(history_path)]
        )

    def _recover_needs_human_task(
        self,
        doing_task: Task,
        result_payload: RalphResultPayload | None,
        *,
        log_path: Path,
        session_id: str | None,
        export_path: Path | None,
        host_branch: str,
    ) -> None:
        try:
            # Move task back from doing to todo in main first.
            main_doing = self.paths.task_path("doing", doing_task.slug)
            if main_doing.exists():
                main_task = parse_task_file(main_doing)
                move_task(main_task, self.paths.task_dir("todo"))
                self.git.commit_all_if_needed(MSG_RECOVER_NEEDS_HUMAN.format(slug=doing_task.slug))
            # Reset the worktree to reflect main's new state.
            if self.paths.worktree_dir.exists():
                wt_git = GitRepo(self.paths.worktree_dir)
                self._sync_worktree(wt_git, host_branch=host_branch)
            todo_path = self.paths.task_path("todo", doing_task.slug)
            if todo_path.exists():
                todo_task = parse_task_file(todo_path)
                human_task = self._create_needs_human_task(
                    todo_task, result_payload, log_path=log_path, session_id=session_id, export_path=export_path
                )
                blocked_task = self._block_task_on_dependency(todo_task, human_task.slug)
                self._write_task_file(blocked_task)
                self.git.commit_all_if_needed(MSG_ESCALATE_HUMAN.format(slug=doing_task.slug))
                active_attempt = self.state_store.load().active_attempt
                self.timeline.record(
                    TimelineEvent(
                        ts=TimelineStore.now_iso(),
                        event="task_escalated",
                        task=doing_task.slug,
                        detail={
                            "attempt": (active_attempt.number if active_attempt is not None else None),
                            "blocker": (result_payload.blocker if result_payload is not None else None),
                            "human_task": human_task.slug,
                            "session_id": session_id,
                        },
                    )
                )
            self._reset_runtime_state()
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="recovery_completed",
                    task=doing_task.slug,
                    detail={"reason": "needs_human"},
                )
            )
        except Exception as recovery_error:
            self._record_recovery_failure(
                task_slug=doing_task.slug, phase="recover-needs-human-task", error=recovery_error
            )
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="cleanup_failed",
                    task=doing_task.slug,
                    detail={
                        "phase": "recover-needs-human-task",
                        "error_type": type(recovery_error).__name__,
                        "error": str(recovery_error),
                    },
                )
            )

    def _resolve_inspect_attempt(self, slug: str | None) -> AttemptState:
        state = self.state_store.load()
        if slug is None:
            if state.active_attempt is not None:
                return state.active_attempt
            if state.attempts:
                latest = state.attempts[-1]
                return self._best_inspect_attempt([latest, *self._load_attempt_history(latest.task_slug)])
            raise JriError("no task attempts recorded")
        matches = [attempt for attempt in state.attempts if attempt.task_slug == slug]
        if state.active_attempt is not None and state.active_attempt.task_slug == slug:
            matches.append(state.active_attempt)
        matches.extend(self._load_attempt_history(slug))
        if not matches:
            raise JriError(f"task '{slug}' has no recorded attempts")
        return self._best_inspect_attempt(matches)

    def _best_inspect_attempt(self, attempts: list[AttemptState]) -> AttemptState:
        latest_number = max(attempt.number for attempt in attempts)
        latest_attempts = [attempt for attempt in attempts if attempt.number == latest_number]
        inspectable = [attempt for attempt in latest_attempts if self._attempt_log_exists(attempt)]
        if inspectable:
            return inspectable[-1]
        return latest_attempts[-1]

    def _attempt_log_exists(self, attempt: AttemptState) -> bool:
        if attempt.log_path is None:
            return False
        return self._resolve_attempt_log_path(attempt.log_path).exists()

    def _resolve_attempt_log_path(self, log_path: str) -> Path:
        path = Path(log_path)
        if path.is_absolute():
            return path
        return self.root / path

    def _inspect_log_path(self, attempt: AttemptState) -> Path:
        if attempt.log_path is not None:
            log_path = self._resolve_attempt_log_path(attempt.log_path)
            if log_path.exists():
                return log_path
        return self._recover_missing_inspect_log(attempt)

    def _recover_missing_inspect_log(self, attempt: AttemptState) -> Path:
        log_path = self.paths.ralph_log_path(f"{attempt.task_slug}-inspect-recovered", int(time.time()))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "\n".join((
                "JRI recovered missing inspect log.",
                f"Task: {attempt.task_slug}",
                f"Attempt: {attempt.number}",
                f"Result: {attempt.result or 'unknown'}",
                f"Original log path: {attempt.log_path or 'not recorded'}",
            ))
            + "\n",
            encoding="utf-8",
        )
        recovered_attempt = replace(attempt, log_path=str(log_path))
        self._save_recovered_inspect_attempt(recovered_attempt)
        self.timeline.record(
            TimelineEvent(
                ts=TimelineStore.now_iso(),
                event="recovery_completed",
                task=attempt.task_slug,
                detail={"reason": "inspect_log_missing", "attempt": attempt.number, "log_path": str(log_path)},
            )
        )
        return log_path

    def _save_recovered_inspect_attempt(self, attempt: AttemptState) -> None:
        state = self.state_store.load()
        attempts = [attempt if self._same_attempt(existing, attempt) else existing for existing in state.attempts]
        already_recorded = any(self._same_attempt(existing, attempt) for existing in state.attempts)
        if not already_recorded:
            attempts.append(attempt)
        active_attempt = state.active_attempt
        if active_attempt is not None and self._same_attempt(active_attempt, attempt):
            active_attempt = attempt
        self.state_store.save(replace(state, active_attempt=active_attempt, attempts=attempts))
        self._persist_attempt_history(attempt)

    def _same_attempt(self, left: AttemptState, right: AttemptState) -> bool:
        return left.task_slug == right.task_slug and left.number == right.number

    def _load_attempt_history(self, slug: str) -> list[AttemptState]:
        history_path = self.paths.attempt_history_path(slug)
        if not history_path.exists():
            return []
        payload: object = yaml.safe_load(history_path.read_text(encoding="utf-8"))
        attempts = cast(dict[str, object], payload).get("attempts") if isinstance(payload, dict) else None
        if not isinstance(attempts, list):
            return []
        return [
            AttemptState.from_payload({**cast(dict[str, object], item), "task_slug": slug})
            for item in cast(list[object], attempts)
            if isinstance(item, dict)
        ]

    def _save_runtime_process(self, *, child_pid: int | None, task_log_path: Path) -> None:
        state = self.state_store.load()
        process = state.process
        tracked_log_path: Path | None = task_log_path
        detached = False
        if process is not None and process.loop_pid == os.getpid():
            detached = process.detached
            if process.log_path:
                tracked_log_path = Path(process.log_path)
        self.state_store.save_process(
            loop_pid=os.getpid(), child_pid=child_pid, log_path=tracked_log_path, detached=detached
        )

    def _set_tracked_process_detached(self, *, detached: bool) -> None:
        state = self.state_store.load()
        process = state.process
        if process is None:
            return
        self.state_store.save_process(
            loop_pid=process.loop_pid,
            child_pid=process.child_pid,
            log_path=Path(process.log_path) if process.log_path else None,
            detached=detached,
        )

    def _follow_log(
        self,
        log_path: Path,
        *,
        loop_pid: int | None,
        loop_process: subprocess.Popen[str] | subprocess.Popen[bytes] | None = None,
        allow_detach: bool,
    ) -> bool:
        footer_enabled = allow_detach and supports_interactive_footer()
        footer_visible = False
        footer_text = ""
        footer_height: int | None = None
        renderer: SavedLogRenderer | None = None

        def _clear_footer() -> None:
            nonlocal footer_height, footer_visible, footer_text
            if not footer_enabled or not footer_visible:
                return
            sys.stdout.write(follow_status_bar_clear(height=footer_height))
            sys.stdout.flush()
            footer_visible = False
            footer_text = ""
            footer_height = None

        def _render_footer(controls: _FollowControls) -> None:
            nonlocal footer_height, footer_visible, footer_text
            if not footer_enabled:
                return
            terminal_size = shutil.get_terminal_size((80, 24))
            if footer_visible and footer_height != terminal_size.lines:
                sys.stdout.write(follow_status_bar_clear(height=footer_height))
                footer_visible = False
                footer_text = ""
                footer_height = None
            next_text = follow_status_bar(
                self._current_follow_task(),
                stop_requested=self.paths.stop_signal_path.exists(),
                confirming_halt=controls.confirming_halt,
                halt_armed=controls.halt_armed,
                activity=renderer.active_task_detail if renderer is not None else None,
                spinner_frame=(
                    "|/-\\"[int(time.monotonic() * 10) % 4]
                    if renderer is not None and renderer.active_task_detail is not None
                    else None
                ),
                width=terminal_size.columns,
                height=terminal_size.lines,
            )
            if footer_visible and next_text == footer_text:
                return
            sys.stdout.write(next_text)
            sys.stdout.flush()
            footer_visible = True
            footer_text = next_text
            footer_height = terminal_size.lines

        with self._follow_control_monitor(enabled=footer_enabled) as controls:
            controls.stop_requested = self.paths.stop_signal_path.exists()
            while True:
                action = controls.poll_action()
                if action == "detach":
                    _clear_footer()
                    print(cyan(_DETACH_NOTICE))
                    sys.stdout.flush()
                    return True
                if action == "stop":
                    self.stop()
                if action == "stop_cancel":
                    self.cancel_stop()
                if action == "halt":
                    _clear_footer()
                    self.halt()
                    return False
                if log_path.exists():
                    break
                _render_footer(controls)
                if loop_pid is None or not self._is_pid_alive(loop_pid):
                    _clear_footer()
                    return False
                time.sleep(0.05)

            with log_path.open("r", encoding="utf-8") as handle:
                renderer = SavedLogRenderer(cwd_hint=str(self.root).rstrip("/") + "/")
                while True:
                    chunk = handle.read()
                    if chunk:
                        rendered = renderer.render_chunk(chunk)
                    else:
                        rendered = ""
                    if rendered:
                        _clear_footer()
                        sys.stdout.write(rendered)
                        sys.stdout.flush()
                    action = controls.poll_action()
                    if action == "detach":
                        _clear_footer()
                        print(cyan(_DETACH_NOTICE))
                        sys.stdout.flush()
                        return True
                    if action == "stop":
                        self.stop()
                    if action == "stop_cancel":
                        self.cancel_stop()
                    if action == "halt":
                        _clear_footer()
                        self.halt()
                        return False
                    process_exited = loop_process is not None and loop_process.poll() is not None
                    if process_exited or loop_pid is None or not self._is_pid_alive(loop_pid):
                        chunk = handle.read()
                        rendered = renderer.render_chunk(chunk, final=True)
                        if rendered:
                            _clear_footer()
                            sys.stdout.write(rendered)
                            sys.stdout.flush()
                        _clear_footer()
                        return False
                    _render_footer(controls)
                    time.sleep(0.1)

    @contextmanager
    def _follow_control_monitor(self, *, enabled: bool) -> Generator[_FollowControls]:
        controls = _FollowControls(enabled=False)
        if not enabled:
            yield controls
            return
        try:
            fd = sys.stdin.fileno()
            previous = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except (AttributeError, OSError, termios.error, ValueError):
            yield controls
            return
        controls.enabled = True
        controls.fd = fd

        try:
            yield controls
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)

    def _current_follow_task(self) -> str | None:
        state = self.state_store.load()
        if state.active_attempt is not None and state.active_attempt.finished_at is None:
            return state.active_attempt.task_slug
        return state.current_task

    def _create_needs_human_task(
        self,
        original_task: Task,
        result_payload: RalphResultPayload | None,
        *,
        log_path: Path,
        session_id: str | None,
        export_path: Path | None,
    ) -> Task:
        if result_payload is None or result_payload.human_task is None:
            raise JriError("needs_human result is missing human_task payload")
        slug = self._allocate_needs_human_slug(original_task.slug)
        human_task = result_payload.human_task
        task = Task(
            path=self.paths.task_path("todo", slug),
            slug=slug,
            metadata=TaskMetadata(
                title=human_task.title[:_MAX_TASK_TITLE_LENGTH].rstrip(),
                priority=(human_task.priority if human_task.priority is not None else original_task.metadata.priority),
                assignee="Human",
                depends_on=[],
                acceptance_criteria=human_task.acceptance_criteria,
            ),
            body=self._needs_human_body(
                original_task,
                result_payload=result_payload,
                human_task=human_task,
                log_path=log_path,
                session_id=session_id,
                export_path=export_path,
            ),
        )
        self._write_task_file(task)
        return task

    def _save_diff_artifact(self, task_slug: str) -> None:
        """Save a diff artifact from this task's begin commit to HEAD."""
        host_branch = self.git.host_branch()
        reset_point = self.state_store.reset_point_for(host_branch=host_branch, task_slug=task_slug)
        if reset_point is None:
            return
        diff_text = self.git.diff(reset_point.begin_commit, "HEAD")
        diff_path = self.paths.diff_artifact_path(task_slug)
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(diff_text, encoding="utf-8")

    def _ensure_lifecycle_task_pristine(self, task: Task, *, baseline: str) -> None:
        if task.path.read_text(encoding="utf-8") == baseline:
            return
        relative_path = self.git.relative_path(task.path)
        raise JriError(
            f"lifecycle task file `{relative_path}` was modified in place; create a follow-up todo task instead"
        )

    def _block_task_on_dependency(self, task: Task, dependency_slug: str) -> Task:
        depends_on = list(task.metadata.depends_on)
        if dependency_slug not in depends_on:
            depends_on.append(dependency_slug)
        return replace(task, metadata=replace(task.metadata, depends_on=depends_on))

    def _write_task_file(self, task: Task) -> None:
        task.path.parent.mkdir(parents=True, exist_ok=True)
        task.path.write_text(dump_task(task), encoding="utf-8")

    def _allocate_needs_human_slug(self, original_slug: str) -> str:
        base = f"{original_slug}--needs-human"
        used = {path.stem for status in _TRACKED_TASK_DIRS for path in self.paths.task_dir(status).glob("*.md")}
        if base not in used:
            return base
        suffix = 2
        while f"{base}-{suffix}" in used:
            suffix += 1
        return f"{base}-{suffix}"

    def _needs_human_body(
        self,
        original_task: Task,
        *,
        result_payload: RalphResultPayload,
        human_task: HumanTaskPayload,
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
            "\n".join((
                f"Ralph reported `needs_human` while working on `{original_path}`.",
                "",
                f"Complete this task to unblock `{original_task.slug}`.",
                "",
                "## Blocker",
                result_payload.blocker or "Not provided.",
                "",
                "## Requested human work",
                human_task.body,
                "",
                "## Original Ralph task",
                f"- Slug: `{original_task.slug}`",
                f"- Title: {original_task.metadata.title}",
                f"- Task file: `{original_path}`",
                "",
                "## Ralph summary",
                result_payload.summary or "Not provided.",
                "",
                "## Run artifacts",
                f"- Ralph log: `{log_relative}`",
                f"- Pi session: `{session_label}`",
                f"- Pi export: `{export_relative}`",
                "",
                "## Ralph task description",
                original_task.body,
            )).rstrip()
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


def _load_managed_template(name: str) -> str:
    return files("jri.core.template").joinpath(*_template_resource_parts(name)).read_text(encoding="utf-8")


def _template_resource_parts(name: str) -> tuple[str, ...]:
    parts = Path(name).parts
    if parts and parts[0] == ".jri":
        return parts[1:]
    return parts
