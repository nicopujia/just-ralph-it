import hashlib
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from types import FrameType
from typing import Any

from .agents import (
    AgentRuntime,
    PiRuntime,
    launch_chat,
    render_saved_log,
)
from .agents.client import SavedLogRenderer
from .agents.session import (
    detect_latest_session,
    export_session_if_available,
    list_sessions,
    runtime_env,
)
from .errors import HaltRequested, JriError, RestartRequested
from .git import (
    MSG_CHECK_PROMOTE,
    MSG_ESCALATE_HUMAN,
    MSG_PROMOTE,
    MSG_RALPH_FINALIZE,
    MSG_RALPH_INTEGRATE,
    MSG_RALPH_PARTIAL,
    MSG_RECORD_ATTEMPT_HISTORY,
    MSG_RECOVER_FAILED,
    MSG_RECOVER_NEEDS_HUMAN,
    MSG_RECOVER_STALE,
    MSG_START_BEGIN,
    MSG_START_COMPLETE,
    GitRepo,
    parse_tag_name,
    tag_name,
)
from .metrics import MetricEntry, MetricsStore
from .models import (
    ATTEMPT_RESULT_VALUES,
    TASK_STATUSES,
    AgentRunResult,
    AttemptState,
    HumanTaskPayload,
    ProcessState,
    PromotionRecord,
    RalphResultPayload,
    Result,
    RunOutcome,
    RunSummary,
    State,
    Task,
    TaskMetadata,
)
from .paths import JriPaths
from .state import StateStore
from .tasks import (
    dump_task,
    list_tasks,
    move_task,
    parse_task_file,
    select_next_task,
    validate_draft_promotion,
)
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
    ".jri/tasks/draft/.gitkeep",
    ".jri/tasks/todo/.gitkeep",
    ".jri/tasks/doing/.gitkeep",
    ".jri/tasks/done/.gitkeep",
    ".jri/attempts/.gitkeep",
)
_SCAFFOLD_TEMPLATE_PATHS = (
    ".jri/learnings.md",
    ".jri/tasks/draft/.gitkeep",
    ".jri/tasks/todo/.gitkeep",
    ".jri/tasks/doing/.gitkeep",
    ".jri/tasks/done/.gitkeep",
    ".jri/attempts/.gitkeep",
)
_ROOT_SCAFFOLD_PATHS = ("Makefile",)
_TRACKED_TASK_DIRS = TASK_STATUSES
_MAX_TASK_TITLE_LENGTH = 50
_DRAFT_TASK_PREFIX = ".jri/tasks/draft/"
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
    def __init__(
        self,
        root: Path,
        *,
        agent_runtime: AgentRuntime | None = None,
    ) -> None:
        self.root = root.resolve()
        self.paths = JriPaths(self.root)
        self.git = GitRepo(self.root)
        self.state_store = StateStore(self.paths.state_path)
        self.timeline = TimelineStore(self.paths.timeline_path)
        self.metrics = MetricsStore(self.paths.metrics_path)
        self.agent_runtime = agent_runtime or PiRuntime()
        self._halt_requested = False
        self._previous_agent_model: str | None = None

    def init(
        self,
        *,
        delete: bool,
        commit_message: str,
        branch: str | None = None,
    ) -> None:
        repo_exists = self.git.is_repo()
        requested_branch = (
            self.git.validate_default_branch_name(branch)
            if branch is not None
            else None
        )
        init_branch = requested_branch or "main"
        self.git.init_if_needed(branch=init_branch)

        if repo_exists and requested_branch is not None:
            self.git.checkout_or_create_branch(requested_branch)

        # Check for existing managed directories
        jri_exists = self.paths.jri_dir.exists()

        if jri_exists:
            if delete:
                # Delete mode: remove existing managed files without prompting
                if jri_exists:
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
        commit_paths = list(_INIT_COMMIT_PATHS)
        commit_paths.extend(str(path.relative_to(self.root)) for path in created_files)
        commit_paths = self._commit_paths(commit_paths)
        # Stage all paths first
        self.git.run("add", "-A", "--", *commit_paths)
        # Check if there's anything to commit before committing
        if not self.git.status_short(*commit_paths):
            return
        self.git.run("commit", "-m", commit_message, "--", *commit_paths)

    def chat(
        self,
        extra_args: list[str],
        *,
        fresh: bool = False,
        model: str | None = None,
        validator_model: str | None = None,
        explore_model: str | None = None,
    ) -> int:
        self.ensure_initialized()
        if fresh:
            self.state_store.save_session(None)
        before = {
            session_id
            for session in list_sessions(self.agent_runtime, root=self.root)
            if isinstance((session_id := session.get("id")), str)
        }
        binary = (
            self.agent_runtime.binary
            if isinstance(self.agent_runtime, PiRuntime)
            else "pi"
        )
        is_pi_chat_runtime = isinstance(self.agent_runtime, PiRuntime)
        state = self.state_store.load()
        session_id = state.session
        if is_pi_chat_runtime and session_id is not None and session_id not in before:
            self.state_store.save_session(None)
            session_id = None
        session_dir = self.paths.chat_logs_dir if is_pi_chat_runtime else None
        with runtime_env(
            overrides={
                "interrogator": model,
                "interrogator-validator": validator_model,
                "explore": explore_model,
            },
            included_agents={"interrogator", "interrogator-validator", "explorer"},
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
        detected_session_id = detect_latest_session(
            root=self.root, before=before, sessions=after
        )
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
        self._recover_stale_start_state(
            mode="detached" if detached else "foreground", force=force
        )
        if detached:
            return self._start_detached(
                max_tasks,
                model,
                validator_model,
                general_model,
                explore_model,
                task_timeout,
                dogfood,
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
        if recover:
            self._recover_stale_start_state(mode=mode, force=force)

        if isinstance(self.agent_runtime, PiRuntime):
            return self._run_loop(
                max_tasks,
                task_timeout=task_timeout,
                force=force,
                dogfood=dogfood,
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
                max_tasks,
                task_timeout=task_timeout,
                force=force,
                dogfood=dogfood,
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
        self._recover_stale_start_state(mode="foreground", force=force)
        if isinstance(self.agent_runtime, PiRuntime):
            return self._run_loop_summary(
                max_tasks,
                task_timeout=task_timeout,
                force=force,
                dogfood=dogfood,
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
                max_tasks,
                task_timeout=task_timeout,
                force=force,
                dogfood=dogfood,
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
        self._recover_stale_start_state(mode="foreground", force=force)
        return self._start_followable(
            max_tasks,
            model,
            validator_model,
            general_model,
            explore_model,
            task_timeout,
            force,
            dogfood,
        )

    def attach(self) -> None:
        self.ensure_initialized()
        state = self.state_store.load()
        process = state.process
        if process is None or not process.log_path:
            raise JriError("no Ralph run is available to attach")
        detached = self._follow_log(
            Path(process.log_path),
            loop_pid=process.loop_pid,
            allow_detach=True,
        )
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
            return {
                status: list_tasks(self.paths.task_dir(status), git_repo=self.git)
                for status in _TRACKED_TASK_DIRS
            }
        except ValueError as exc:
            raise JriError(str(exc)) from exc

    def ralph_status_summary(self) -> str:
        self.ensure_initialized()
        state = self.state_store.load()
        process = state.process
        loop_pid = process.loop_pid if process is not None else None
        process_alive = loop_pid is not None and self._is_pid_alive(loop_pid)
        active_task = (
            state.active_attempt.task_slug
            if state.active_attempt is not None
            and state.active_attempt.finished_at is None
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

        return "Ralph: not running"

    def metrics_summary(self) -> str | None:
        """Return a human-readable metrics summary, or None if no metrics."""
        return self.metrics.summary()

    def promote_drafts(self, *, slugs: list[str]) -> list[Task]:
        self.ensure_initialized()

        draft_tasks = self._list_tasks("draft")
        selected = self._select_draft_tasks(draft_tasks, slugs)
        self._validate_selected_drafts_for_promotion(selected, draft_tasks=draft_tasks)
        self._validate_promotion_approval(selected)

        promoted_tasks: list[Task] = []
        for task in selected:
            promoted_task = move_task(task, self.paths.task_dir("todo"))
            promoted_tasks.append(promoted_task)

        self.state_store.clear_promotion()
        self.git.commit_paths_if_needed(
            MSG_PROMOTE,
            [
                self.git.relative_path(self.paths.task_dir("draft")),
                self.git.relative_path(self.paths.task_dir("todo")),
            ],
        )
        return promoted_tasks

    def approve_draft_promotion(self, *, slugs: list[str]) -> list[Task]:
        self.ensure_initialized()

        draft_tasks = self._list_tasks("draft")
        selected = self._select_draft_tasks(draft_tasks, slugs)
        self._validate_selected_drafts_for_promotion(selected, draft_tasks=draft_tasks)
        self.state_store.save_promotion(
            PromotionRecord(
                confirmed_at=int(time.time()),
                task_slugs=[task.slug for task in selected],
                content_digests=self._draft_content_digests(selected),
            )
        )
        self.git.commit_paths_if_needed(
            MSG_CHECK_PROMOTE,
            [self.git.relative_path(self.paths.state_path)],
        )
        return selected

    def check_draft_promotion(self, *, slugs: list[str]) -> list[Task]:
        self.ensure_initialized()

        draft_tasks = self._list_tasks("draft")
        selected = self._select_draft_tasks(draft_tasks, slugs)
        self._validate_selected_drafts_for_promotion(selected, draft_tasks=draft_tasks)
        self.git.commit_paths_if_needed(
            MSG_CHECK_PROMOTE,
            [self.git.relative_path(self.paths.task_dir("draft"))],
        )
        return selected

    def _validate_selected_drafts_for_promotion(
        self, selected: list[Task], *, draft_tasks: list[Task]
    ) -> None:
        try:
            validate_draft_promotion(
                selected,
                all_draft_slugs={task.slug for task in draft_tasks},
                promoted_slugs=self._promoted_task_slugs(),
                promoted_deps=self._promoted_task_deps(),
            )
        except ValueError as exc:
            raise JriError(str(exc)) from exc

    def _draft_content_digests(self, tasks: list[Task]) -> dict[str, str]:
        return {
            task.slug: hashlib.sha256(task.path.read_bytes()).hexdigest()
            for task in tasks
        }

    def _validate_promotion_approval(self, selected: list[Task]) -> None:
        state = self.state_store.load()
        approval = state.promotion
        if approval is None:
            raise JriError("draft promotion must be approved by the validator first")

        selected_slugs = [task.slug for task in selected]
        if approval.task_slugs != selected_slugs:
            raise JriError(
                "draft promotion must match the latest validator-approved draft set"
            )

        current_digests = self._draft_content_digests(selected)
        if approval.content_digests != current_digests:
            self.state_store.clear_promotion()
            raise JriError("draft promotion approval changed since approval")

    def reset(self, target_task: str | None = None) -> None:
        """Reset the repository to the appropriate task tag boundary.

        If target_task is provided, prefer jri/end/{target_task} and fall back to
        jri/begin/{target_task}. Otherwise, find the most recent end tag and fall
        back to the most recent begin tag when no end tag exists.

        Resets to jri/end/{task} keep the tagged commit. Resets to
        jri/begin/{task} land just before the tagged commit.
        """
        self.ensure_initialized()
        state = self.state_store.load()
        target_tag = self._resolve_reset_target_tag(target_task)
        target_ref = self._resolve_reset_target_ref(target_tag)

        self._cleanup_tracked_processes(required=False)
        default = self.git.default_branch(hint=state.branch)
        current = self.git.current_branch()
        if current != default:
            self.git.run("checkout", "-f", default)
        self.git.reset_hard(target_ref)
        # Clean up worktree and Ralph's branch.
        if self.paths.worktree_dir.exists():
            self.git.remove_worktree(self.paths.worktree_dir)
        for branch in self._managed_ralph_branches():
            if self.git.has_local_branch(branch):
                self.git.delete_branch(branch)
        self.state_store.save(
            State(
                finished_at=state.finished_at,
                session=state.session,
                branch=state.branch,
                attempts=state.attempts,
                promotion=state.promotion,
            )
        )

    def _find_latest_tag(self, stage: str) -> str | None:
        """Find the most recent task tag for the given stage.

        Returns the tag name or None if no matching tags exist.
        Uses git for-each-ref to list tags in reverse chronological order.
        """
        result = self.git.run(
            "for-each-ref",
            "--sort=-creatordate",
            "--format=%(refname:short)",
            f"refs/tags/jri/{stage}/",
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        tags = result.stdout.strip().split("\n")
        for tag in tags:
            parsed = parse_tag_name(tag)
            if parsed and parsed[0] == stage:
                return tag
        return None

    def _find_latest_end_tag(self) -> str | None:
        return self._find_latest_tag("end")

    def _find_latest_reset_tag(self) -> str | None:
        return self._find_latest_tag("end") or self._find_latest_tag("begin")

    def _find_reset_tag_for_task(self, task_slug: str) -> str | None:
        for stage in ("end", "begin"):
            candidate = tag_name(task_slug, stage)
            if self.git.has_tag(candidate):
                return candidate
        return None

    def _resolve_reset_target_tag(self, target_task: str | None = None) -> str:
        if target_task:
            target_tag = self._find_reset_tag_for_task(target_task)
            if target_tag is None:
                raise JriError(f"no begin or end tag found for task '{target_task}'")
            return target_tag

        target_tag = self._find_latest_reset_tag()
        if target_tag is None:
            raise JriError("no task tag found — run `jri start` first")
        return target_tag

    def _resolve_reset_target_ref(self, target_tag: str) -> str:
        parsed = parse_tag_name(target_tag)
        if parsed is None:
            return self.git.rev_parse(target_tag)
        stage, _ = parsed
        if stage == "begin":
            return self.git.rev_parse(f"{target_tag}^")
        return self.git.rev_parse(target_tag)

    def _describe_reset_target(self, target_tag: str) -> str:
        parsed = parse_tag_name(target_tag)
        if parsed is None:
            return target_tag
        stage, _ = parsed
        if stage == "begin":
            return f"just before {target_tag}"
        return target_tag

    def ensure_initialized(self) -> None:
        self.git.ensure_repo()
        if not self.paths.jri_dir.exists():
            raise JriError("project is not initialized; run `jri init`")

    def _ensure_not_managed_worktree(self) -> None:
        if self.root.name == "worktree" and self.root.parent.name == ".jri":
            raise JriError(
                "jri start cannot run from .jri/worktree; "
                "run it from the main repository root"
            )

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

    def _select_draft_tasks(
        self, draft_tasks: list[Task], slugs: list[str]
    ) -> list[Task]:
        by_slug = {task.slug: task for task in draft_tasks}
        if not slugs:
            if not draft_tasks:
                raise JriError("no draft tasks selected for promotion")
            return draft_tasks

        requested_slugs = list(dict.fromkeys(slugs))
        missing = [slug for slug in requested_slugs if slug not in by_slug]
        if missing:
            joined = ", ".join(missing)
            raise JriError(f"draft task not found: {joined}")
        return sorted(
            (by_slug[slug] for slug in requested_slugs),
            key=lambda task: task.slug,
        )

    def _promoted_task_slugs(self) -> set[str]:
        slugs: set[str] = set()
        for status in ("todo", "doing", "done"):
            slugs.update(task.slug for task in self._list_tasks(status))
        return slugs

    def _promoted_task_deps(self) -> dict[str, list[str]]:
        deps: dict[str, list[str]] = {}
        for status in ("todo", "doing", "done"):
            for task in self._list_tasks(status):
                deps[task.slug] = list(task.metadata.depends_on)
        return deps

    def _default_branch(self) -> str:
        return self.git.default_branch(hint=self.state_store.load().branch)

    def _ralph_branch(self) -> str:
        return self.git.ralph_branch(hint=self.state_store.load().branch)

    def _managed_ralph_branches(self) -> tuple[str, ...]:
        default = self._default_branch()
        return (f"ralph/{default}", f"ralph-{default}")

    def has_managed_ralph_branch(self) -> bool:
        return any(
            self.git.has_local_branch(branch)
            for branch in ("ralph", *self._managed_ralph_branches())
        )

    def _is_managed_ralph_branch(self, branch: str) -> bool:
        return branch == "ralph" or branch in self._managed_ralph_branches()

    def _create_scaffold(self) -> list[Path]:
        created_files: list[Path] = []
        self.paths.jri_dir.mkdir(parents=True, exist_ok=True)

        self._write_template_files(_SCAFFOLD_TEMPLATE_PATHS)
        created_files.extend(self._write_root_scaffold_files())
        self._write_gitignore_file()
        self.state_store.initialize(branch=self.git.current_branch() or None)
        return created_files

    _GITIGNORE_CONTENT = "logs/\nsignals/\n*state.json*\nmetrics.json\nworktree/\n"

    def _write_gitignore_file(self) -> None:
        self.paths.gitignore_path.write_text(
            self._GITIGNORE_CONTENT,
            encoding="utf-8",
        )

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
            command,
            cwd=self.root,
            env=env,
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
            command,
            cwd=self.root,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        log_file.close()
        self.state_store.save_process(
            loop_pid=process.pid,
            child_pid=None,
            log_path=run_log_path,
            detached=False,
        )
        detached = self._follow_log(
            run_log_path,
            loop_pid=process.pid,
            loop_process=process,
            allow_detach=True,
        )
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
        model_overrides: dict[str, str | None] | None = None,
    ) -> int:
        return self._run_loop_summary(
            max_tasks,
            task_timeout=task_timeout,
            force=force,
            dogfood=dogfood,
            model_overrides=model_overrides,
        ).completed

    def _run_loop_summary(
        self,
        max_tasks: int | None,
        task_timeout: int | None = None,
        force: bool = False,
        dogfood: bool = False,
        model_overrides: dict[str, str | None] | None = None,
    ) -> RunSummary:
        try:
            doing = list_tasks(self.paths.task_dir("doing"), git_repo=self.git)
        except ValueError as exc:
            raise JriError(str(exc)) from exc
        if doing:
            raise JriError("a task is already in progress")
        self._handle_dirty_workdir(force=force)
        self._handle_wrong_branch(force=force)
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
                runtime_context = self._start_pi_runtime(
                    overrides=model_overrides or {}
                )
            while max_tasks is None or attempted < max_tasks:
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
                    with self._running_pi_runtime(overrides=model_overrides or {}):
                        result = self._run_task(next_task, task_timeout=task_timeout)
                else:
                    result = self._run_task(next_task, task_timeout=task_timeout)
                attempted += 1
                if result == "completed":
                    completed += 1
                    task_results[next_task.slug] = result
                    outcome = "completed"
                    if self._should_restart_process_after_iteration(
                        dogfood=dogfood,
                        max_tasks=max_tasks,
                        completed=attempted,
                    ):
                        remaining_tasks = (
                            max_tasks - attempted if max_tasks is not None else None
                        )
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
                            detail={
                                "reason": "task_timeout",
                                "limit_seconds": task_timeout,
                            },
                        )
                    )
                    break

                if self.paths.stop_signal_path.exists():
                    self.paths.stop_signal_path.unlink()
                    break

            # Record if we stopped due to task limit
            if (
                max_tasks is not None
                and attempted >= max_tasks
                and outcome != "timeout"
            ):
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

        return RunSummary(
            completed=completed,
            outcome=outcome,
            task_results=task_results,
        )

    def _should_restart_process_after_iteration(
        self,
        *,
        dogfood: bool,
        max_tasks: int | None,
        completed: int,
    ) -> bool:
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
    def _running_pi_runtime(
        self, *, overrides: dict[str, str | None]
    ) -> Iterator[None]:
        runtime = self._start_pi_runtime(overrides=overrides)
        try:
            yield
        finally:
            self._stop_pi_runtime(runtime)

    def _start_pi_runtime(
        self, *, overrides: dict[str, str | None]
    ) -> AbstractContextManager[dict[str, str]]:
        if not isinstance(self.agent_runtime, PiRuntime):
            raise JriError("Pi runtime requested for non-Pi agent runtime")
        result_path = self.paths.jri_dir / "signals" / "result"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        # Ensure the worktree exists before Pi starts so Ralph's tools
        # resolve paths against the worktree, not the main repo.
        wt_git, _ = self._ensure_worktree()
        self._sync_worktree(wt_git)
        runtime = runtime_env(overrides=overrides)
        pi_env = runtime.__enter__()
        previous_model = self.agent_runtime.model
        if overrides.get("ralph") is not None:
            self.agent_runtime.model = overrides["ralph"]
        self._previous_agent_model = previous_model
        try:
            self.agent_runtime.start(
                env={
                    **pi_env,
                    "JRI_RESULT_PATH": str(result_path.resolve()),
                },
                cwd=self.paths.worktree_dir,
            )
        except BaseException as exc:
            self.agent_runtime.model = previous_model
            self._previous_agent_model = None
            runtime.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return runtime

    def _stop_pi_runtime(
        self, runtime: AbstractContextManager[dict[str, str]] | None
    ) -> None:
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

    def _ensure_worktree(self) -> tuple[GitRepo, JriPaths]:
        """Ensure the persistent Ralph worktree exists and return helpers."""
        wt_dir = self.paths.worktree_dir
        branch = self._ralph_branch()

        if not self.git.has_local_branch(branch):
            default_ref = self.git.rev_parse(self._default_branch())
            self.git.run("branch", branch, default_ref)

        if not wt_dir.exists():
            self.git.prune_worktrees()
            self.git.add_worktree(wt_dir, branch)

        return GitRepo(wt_dir), JriPaths(wt_dir)

    def _sync_worktree(self, wt_git: GitRepo) -> None:
        """Reset Ralph's worktree branch to the default-branch tip."""
        branch = self._ralph_branch()
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
        default_ref = self.git.rev_parse(self._default_branch())
        self.git.reset_branch(branch, default_ref)
        wt_git.run("checkout", "--force", branch)
        wt_git.run("clean", "-fd")

    def _is_completed_attempt_payload(self, attempt: AttemptState) -> bool:
        return attempt.result == "completed" or (
            attempt.result_payload is not None
            and attempt.result_payload.result == "completed"
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
        if not self._is_managed_ralph_branch(attempt.branch):
            return False
        if not self.git.has_local_branch(attempt.branch):
            return False
        return not self.git.is_ancestor(attempt.branch, self._default_branch())

    def _integrate_completed_branch(self, *, task_slug: str, branch: str) -> None:
        if not self.git.has_local_branch(branch):
            return
        default = self._default_branch()
        if self.git.is_ancestor(branch, default):
            return
        if self.git.current_branch() != default:
            self.git.checkout(default)
        if self.git.status_short():
            raise JriError(
                "git working tree must be clean before integrating Ralph work"
            )
        if self.git.is_ancestor(default, branch):
            self.git.merge_ff_only(branch)
            return
        self.git.merge_no_ff(branch, message=MSG_RALPH_INTEGRATE.format(slug=task_slug))

    def _previous_attempts_prompt_section(self, task_slug: str) -> str:
        attempts = [
            attempt
            for attempt in self._load_attempt_history(task_slug)
            if attempt.result in {"incompleted", "needs_human", "failed", "timeout"}
        ][-3:]
        if not attempts:
            return ""

        lines = ["Previous attempts:"]
        for attempt in attempts:
            lines.append(f"- Attempt {attempt.number}")
            if attempt.result is not None:
                lines.append(f"  Result: {attempt.result}")
            payload = attempt.result_payload
            if payload is None:
                continue
            if payload.summary:
                lines.append(f"  Summary: {_single_line(payload.summary, limit=240)}")
            if payload.blocker:
                lines.append(f"  Blocker: {_single_line(payload.blocker, limit=240)}")
            if payload.learnings:
                lines.append("  Actionable learnings:")
                for learning in payload.learnings[:5]:
                    lines.append(f"  - {_single_line(learning, limit=220)}")
        rendered = "\n".join(lines)
        return rendered[:2000]

    def _run_task(self, task: Task, task_timeout: int | None = None) -> Result:
        state = self.state_store.load()
        started_at = int(time.time())
        log_path = self.paths.ralph_log_path(task.slug, started_at)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(exist_ok=True)
        branch = self._ralph_branch()
        print(task_header(task.slug))
        sys.stdout.flush()

        # Calculate deadline if task_timeout is set
        deadline: int | None = None
        if task_timeout is not None and task_timeout > 0:
            deadline = started_at + task_timeout

        wt_git, wt_paths = self._ensure_worktree()

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
                detail={
                    "attempt": attempt.number,
                    "branch": branch,
                    "log_path": str(log_path),
                },
            )
        )
        # Move task to doing in the MAIN repo first, commit, then sync the
        # worktree so it inherits the move via the default-branch reset.
        # This keeps `jri status` and the task state machine in main.
        main_doing_task = move_task(task, self.paths.task_dir("doing"))
        self.git.commit_all_if_needed(MSG_START_BEGIN.format(slug=task.slug))
        # Create begin tag for this task
        # (delete first if it exists from previous attempt)
        begin_tag = tag_name(task.slug, "begin")
        if self.git.has_tag(begin_tag):
            self.git.run("tag", "-d", begin_tag, check=False)
        self.git.create_tag(begin_tag)
        self._sync_worktree(wt_git)
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
        on_start_cb = lambda child_pid: self._save_runtime_process(  # noqa: E731
            child_pid=child_pid,
            task_log_path=log_path,
        )
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
                    ts=TimelineStore.now_iso(),
                    event="stderr_warning",
                    task=task.slug,
                    detail={"message": message},
                )
            )
            self._recover_failed_task_wt(doing_task, wt_git)
            self._finish_attempt(attempt, result="failed")
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="task_failed",
                    task=task.slug,
                    detail={
                        "reason": "agent_runtime_exception",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            )
            print(task_footer("failed"))
            sys.stdout.flush()
            return "failed"

        attempt = replace(
            attempt,
            session_id=result.session_id,
            result_payload=result.payload,
        )
        self.state_store.save_active_attempt(attempt)

        # Check for task timeout
        finished_at = int(time.time())
        if result.result == "timeout" or (
            deadline is not None and finished_at > deadline
        ):
            timeout_msg = (
                f"Task {task.slug} exceeded timeout of {task_timeout}s "
                f"(took {finished_at - started_at}s)"
            )
            print(timeout_msg, file=sys.stderr)
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="stderr_warning",
                    task=task.slug,
                    detail={"message": timeout_msg},
                )
            )
            self._recover_failed_task_wt(doing_task, wt_git)
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
                    ts=TimelineStore.now_iso(),
                    event="stderr_warning",
                    task=task.slug,
                    detail={"message": warning},
                )
            )

        payload_violation = self._result_payload_violation(result)
        if payload_violation is not None:
            self._recover_failed_task_wt(doing_task, wt_git)
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
            self._recover_failed_task_wt(doing_task, wt_git)
            self._finish_attempt(attempt, result="failed")
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="task_failed",
                    task=task.slug,
                    detail={
                        "reason": "nonzero_returncode",
                        "returncode": result.returncode,
                    },
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
            self._recover_failed_task_wt(doing_task, wt_git)
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
            )
            self._finish_attempt(attempt, result="needs_human")
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="task_needs_human",
                    task=task.slug,
                )
            )
            print(task_footer("needs_human"))
            sys.stdout.flush()
            return "needs_human"

        if result.result == "incompleted" and (
            result.payload is None or not result.payload.learnings
        ):
            self._recover_failed_task_wt(doing_task, wt_git)
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
            self._recover_failed_task_wt(doing_task, wt_git)
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
                check = subprocess.run(
                    ["make", "check"],
                    cwd=wt_paths.root,
                    capture_output=True,
                    text=True,
                )
            except FileNotFoundError:
                make_msg = "make: command not found"
                print(make_msg, file=sys.stderr)
                self.timeline.record(
                    TimelineEvent(
                        ts=TimelineStore.now_iso(),
                        event="stderr_warning",
                        task=task.slug,
                        detail={"message": make_msg},
                    )
                )
                self._recover_failed_task_wt(doing_task, wt_git)
                self.metrics.record(
                    MetricEntry(
                        task=task.slug,
                        ts=MetricsStore.now_iso(),
                        result="fail",
                    )
                )
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
                        detail={
                            "message": make_fail_msg[:500] if make_fail_msg else ""
                        },
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
                self._recover_failed_task_wt(doing_task, wt_git)
                self.metrics.record(
                    MetricEntry(
                        task=task.slug,
                        ts=MetricsStore.now_iso(),
                        result="fail",
                    )
                )
                self._finish_attempt(attempt, result="failed")
                print(task_footer("failed"))
                sys.stdout.flush()
                return "failed"
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="make_check_passed",
                    task=task.slug,
                )
            )
            self.metrics.record(
                MetricEntry(
                    task=task.slug,
                    ts=MetricsStore.now_iso(),
                    result="pass",
                )
            )

        finished_at = int(time.time())
        attempt = replace(attempt, finished_at=finished_at, result="completed")
        self.state_store.save_active_attempt(attempt)

        self._integrate_completed_branch(task_slug=task.slug, branch=branch)

        if not (self.paths.task_path("doing", task.slug)).exists():
            relative_path = f".jri/tasks/doing/{task.slug}.md"
            raise JriError(f"task file `{relative_path}` disappeared during Ralph run")
        doing_on_main = parse_task_file(self.paths.task_path("doing", task.slug))
        move_task(doing_on_main, self.paths.task_dir("done"))
        self.git.commit_all_if_needed(MSG_START_COMPLETE.format(slug=task.slug))
        # Create end tag for this task (delete first if it exists from previous attempt)
        end_tag = tag_name(task.slug, "end")
        if self.git.has_tag(end_tag):
            self.git.run("tag", "-d", end_tag, check=False)
        self.git.create_tag(end_tag)
        self._save_diff_artifact(task.slug)

        if self.git.has_remote():
            self.git.push_task_refs(branch=branch, tag=end_tag)

        self.state_store.save_active_attempt(attempt)
        self._persist_attempt_history(attempt)
        self.state_store.mark_task_finished(
            task_slug=task.slug,
            finished_at=finished_at,
        )
        self.state_store.clear_active_attempt()
        self.timeline.record(
            TimelineEvent(
                ts=TimelineStore.now_iso(),
                event="task_completed",
                task=task.slug,
            )
        )
        print(task_footer("completed"))
        sys.stdout.flush()
        return "completed"

    def _recover_failed_task_wt(self, doing_task: Task, wt_git: GitRepo) -> None:
        """Recover from a failed task in the worktree."""
        try:
            # Move task back to todo on main repo first.
            main_doing = self.paths.task_path("doing", doing_task.slug)
            if main_doing.exists():
                main_task = parse_task_file(main_doing)
                move_task(main_task, self.paths.task_dir("todo"))
                self.git.commit_all_if_needed(
                    MSG_RECOVER_FAILED.format(slug=doing_task.slug)
                )
            # Reset the worktree so it reflects main's new state.
            self._sync_worktree(wt_git)
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
            self._record_recovery_failure(
                task_slug=doing_task.slug,
                phase="recover-failed-task",
                error=recovery_error,
            )
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
        if self._dirty_paths_are_draft_only(status):
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

    def _dirty_paths_are_draft_only(self, status: str) -> bool:
        paths = [path for path in self._status_paths(status) if path]
        return bool(paths) and all(
            path.startswith(_DRAFT_TASK_PREFIX) for path in paths
        )

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

    def _handle_wrong_branch(self, *, force: bool) -> None:
        """Handle being on the wrong branch before starting the loop."""
        state = self.state_store.load()
        default = self.git.default_branch(hint=state.branch)
        current = self.git.current_branch()
        if current == default:
            return
        if force:
            self.git.run("checkout", default)
            return
        sys.stdout.write(f'Currently on branch "{current}", expected "{default}".\n')
        sys.stdout.write(f"Switch to {default}? [Y/n] ")
        sys.stdout.flush()
        choice = input().strip().lower()
        if choice in ("", "y"):
            self.git.run("checkout", default)
        else:
            raise JriError("aborted by user")

    def _recover_stale_start_state(self, *, mode: str, force: bool = False) -> None:
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
            raise JriError(
                "a Ralph process is already running; use `jri attach` to follow it"
            )

        if doing_tasks:
            if not force:
                slug = doing_tasks[0].slug
                sys.stdout.write(
                    f'Task "{slug}" has incomplete work from a crashed run.\n'
                )
                sys.stdout.write("Reset and move back to todo? [Y/n] ")
                sys.stdout.flush()
                choice = input().strip().lower()
                if choice not in ("", "y"):
                    raise JriError("aborted by user")
            reason = (
                "dead-tracked-process" if loop_pid is not None else "no-tracked-process"
            )
            active_attempt = state.active_attempt
            if active_attempt is not None:
                if not self._attempt_matches_task(active_attempt, doing_tasks[0]):
                    raise JriError("active attempt does not match the task in progress")
                completion_evidence = self._attempt_completion_evidence(active_attempt)
                if completion_evidence is not None:
                    self._record_recovery(
                        mode=mode,
                        reason="resume-completed-attempt",
                        task_slug=doing_tasks[0].slug,
                        process=process,
                        evidence=completion_evidence,
                    )
                    self._complete_attempt(active_attempt, doing_task=doing_tasks[0])
                    return
                if active_attempt.result == "completed":
                    reason = "missing-completion-evidence"
            self._recover_stale_task(
                doing_tasks[0],
                mode=mode,
                reason=reason,
                process=process,
            )
            return

        if state.active_attempt is not None:
            active_attempt = state.active_attempt
            completion_evidence = self._attempt_completion_evidence(active_attempt)
            if completion_evidence is not None:
                self._record_recovery(
                    mode=mode,
                    reason="resume-completed-attempt",
                    task_slug=active_attempt.task_slug,
                    process=process,
                    evidence=completion_evidence,
                )
                self._complete_attempt(active_attempt, doing_task=None)
                return
            task_status = self._tracked_task_status(active_attempt.task_slug)
            if task_status in {"doing", "done"}:
                self._recover_unverified_completed_attempt(
                    active_attempt,
                    mode=mode,
                    reason="missing-completion-evidence",
                    process=process,
                )
                return
            if active_attempt.result in {
                "failed",
                "incompleted",
                "needs_human",
                "interrupted",
            }:
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
                reason="stale-task-state",
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

    def _recover_stale_task(
        self,
        doing_task: Task,
        *,
        mode: str,
        reason: str,
        process: ProcessState | None,
    ) -> None:
        try:
            default = self._default_branch()
            current_branch = self.git.current_branch()

            if current_branch == default:
                if self.git.status_short():
                    raise JriError(
                        "git working tree must be clean before stale recovery"
                    )
            elif self._is_managed_ralph_branch(current_branch):
                self.git.commit_all_if_needed(
                    MSG_RALPH_PARTIAL.format(slug=doing_task.slug)
                )
                self.git.checkout(default)
            else:
                raise JriError(f"jri start must begin from the {default} branch")

            # Reset worktree if it exists
            if self.paths.worktree_dir.exists():
                wt_git = GitRepo(self.paths.worktree_dir)
                self._sync_worktree(wt_git)

            move_task(doing_task, self.paths.task_dir("todo"))
            self._record_recovery(
                mode=mode,
                reason=reason,
                task_slug=doing_task.slug,
                process=process,
            )
            self._mark_active_attempt_interrupted()
            self._reset_runtime_state()
            self.git.commit_all_if_needed(
                MSG_RECOVER_STALE.format(slug=doing_task.slug)
            )
        except Exception as recovery_error:
            self._record_recovery_failure(
                task_slug=doing_task.slug,
                phase="recover-stale-task",
                error=recovery_error,
            )
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
            state.active_attempt,
            finished_at=state.active_attempt.finished_at or int(time.time()),
            result="interrupted",
        )
        self.state_store.save_active_attempt(attempt)
        self._persist_attempt_history(attempt)
        self.state_store.clear_active_attempt()

    def _attempt_matches_task(self, attempt: AttemptState, task: Task) -> bool:
        branch_ok = self._is_managed_ralph_branch(attempt.branch)
        return attempt.task_slug == task.slug and branch_ok

    def _attempt_completion_evidence(
        self, attempt: AttemptState
    ) -> dict[str, str] | None:
        evidence: dict[str, str] = {}
        history_entry = next(
            (
                entry
                for entry in self._load_attempt_history(attempt.task_slug)
                if entry.number == attempt.number
                and entry.result == "completed"
                and entry.finished_at is not None
            ),
            None,
        )
        if history_entry is not None:
            evidence["attempt_history"] = (
                f"{self.git.relative_path(self.paths.attempt_history_path(attempt.task_slug))}"
                f"#{attempt.number}"
            )

        if (self.root / "Makefile").exists():
            make_check_passed_ts = self._timeline_event_ts(
                task_slug=attempt.task_slug,
                event="make_check_passed",
                not_before=attempt.started_at,
            )
            if make_check_passed_ts is not None:
                evidence["make_check_passed_event"] = make_check_passed_ts

        if self.git.has_tag(tag_name(attempt.task_slug, "end")):
            evidence["end_tag"] = tag_name(attempt.task_slug, "end")

        if self.paths.task_path("done", attempt.task_slug).exists():
            evidence["task_status"] = "done"

        if self._is_completed_attempt_payload(
            attempt
        ) and self._attempt_has_unintegrated_branch_work(attempt):
            evidence["branch_work"] = attempt.branch

        if "attempt_history" in evidence and (
            "make_check_passed_event" in evidence or "end_tag" in evidence
        ):
            return evidence
        if "branch_work" in evidence and self._is_completed_attempt_payload(attempt):
            return evidence
        if {
            "task_status",
            "end_tag",
            "make_check_passed_event",
        }.issubset(evidence):
            return evidence
        return None

    def _timeline_event_ts(
        self,
        *,
        task_slug: str,
        event: str,
        not_before: int | None,
    ) -> str | None:
        minimum_ts = (
            datetime.fromtimestamp(not_before, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if not_before is not None
            else None
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
        self,
        attempt: AttemptState,
        *,
        mode: str,
        reason: str,
        process: ProcessState | None,
    ) -> None:
        try:
            default = self._default_branch()
            current_branch = self.git.current_branch()

            if current_branch == default:
                if self.git.status_short():
                    raise JriError(
                        "git working tree must be clean before stale recovery"
                    )
            elif self._is_managed_ralph_branch(current_branch):
                self.git.commit_all_if_needed(
                    MSG_RALPH_PARTIAL.format(slug=attempt.task_slug)
                )
                self.git.checkout(default)
            else:
                raise JriError(f"jri start must begin from the {default} branch")

            if self.paths.worktree_dir.exists():
                wt_git = GitRepo(self.paths.worktree_dir)
                self._sync_worktree(wt_git)

            task_moved = False
            for status in ("doing", "done"):
                task_path = self.paths.task_path(status, attempt.task_slug)
                if not task_path.exists():
                    continue
                move_task(parse_task_file(task_path), self.paths.task_dir("todo"))
                task_moved = True
                break

            self._record_recovery(
                mode=mode,
                reason=reason,
                task_slug=attempt.task_slug,
                process=process,
            )
            self._mark_active_attempt_interrupted()
            self._reset_runtime_state()
            if task_moved:
                self.git.commit_all_if_needed(
                    MSG_RECOVER_STALE.format(slug=attempt.task_slug)
                )
        except Exception as recovery_error:
            self._record_recovery_failure(
                task_slug=attempt.task_slug,
                phase="recover-unverified-completed-attempt",
                error=recovery_error,
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
                    MSG_RALPH_PARTIAL.format(slug=attempt.task_slug)
                )
            self.git.checkout(default)
        elif current_branch != default:
            raise JriError(f"jri start must begin from the {default} branch")

        self._integrate_completed_branch(
            task_slug=attempt.task_slug,
            branch=attempt.branch,
        )

        if doing_task is not None and doing_task.path.exists():
            move_task(doing_task, self.paths.task_dir("done"))
        self.git.commit_all_if_needed(MSG_START_COMPLETE.format(slug=attempt.task_slug))
        # Create end tag for this task (delete first if it exists from previous attempt)
        end_tag = tag_name(attempt.task_slug, "end")
        if self.git.has_tag(end_tag):
            self.git.run("tag", "-d", end_tag, check=False)
        self.git.create_tag(end_tag)
        self._save_diff_artifact(attempt.task_slug)
        if self.git.has_remote() and self.git.has_local_branch(attempt.branch):
            self.git.push_task_refs(branch=attempt.branch, tag=end_tag)

        finished_at = attempt.finished_at or int(time.time())
        attempt = replace(attempt, finished_at=finished_at, result="completed")
        self.state_store.save_active_attempt(attempt)
        self._persist_attempt_history(attempt)
        self.state_store.mark_task_finished(
            task_slug=attempt.task_slug,
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
            evidence_summary = ",".join(
                f"{key}:{value}" for key, value in sorted(evidence.items())
            )
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

    def _record_recovery_failure(
        self,
        *,
        task_slug: str | None,
        phase: str,
        error: Exception,
    ) -> None:
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        error_type = type(error).__name__
        error_message = str(error).replace("\n", "\\n")
        line = " ".join(
            (
                timestamp,
                "event=recovery-failure",
                f"task={task_slug or '-'}",
                f"phase={phase}",
                f"error_type={error_type}",
                f"error_message={error_message}",
            )
        )
        self.paths.recovery_failures_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.recovery_failures_log_path.open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(line + "\n")

    def _persist_attempt_history(self, attempt: AttemptState) -> None:
        history_path = self.paths.attempt_history_path(attempt.task_slug)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        if history_path.exists():
            payload = json.loads(history_path.read_text(encoding="utf-8"))
            attempts = payload.get("attempts") if isinstance(payload, dict) else None
            history = (
                [item for item in attempts if isinstance(item, dict)]
                if isinstance(attempts, list)
                else []
            )
        else:
            history = []
        serialized = attempt.to_payload()
        updated = False
        for index, existing in enumerate(history):
            if existing.get("number") == attempt.number:
                history[index] = serialized
                updated = True
                break
        if not updated:
            history.append(serialized)
        history_path.write_text(
            json.dumps(
                {"task_slug": attempt.task_slug, "attempts": history},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.git.commit_paths_if_needed(
            MSG_RECORD_ATTEMPT_HISTORY.format(slug=attempt.task_slug),
            [self.git.relative_path(history_path)],
        )

    def _recover_needs_human_task(
        self,
        doing_task: Task,
        result_payload: RalphResultPayload | None,
        *,
        log_path: Path,
        session_id: str | None,
        export_path: Path | None,
    ) -> None:
        try:
            # Move task back from doing to todo in main first.
            main_doing = self.paths.task_path("doing", doing_task.slug)
            if main_doing.exists():
                main_task = parse_task_file(main_doing)
                move_task(main_task, self.paths.task_dir("todo"))
                self.git.commit_all_if_needed(
                    MSG_RECOVER_NEEDS_HUMAN.format(slug=doing_task.slug)
                )
            # Reset the worktree to reflect main's new state.
            if self.paths.worktree_dir.exists():
                wt_git = GitRepo(self.paths.worktree_dir)
                self._sync_worktree(wt_git)
            todo_path = self.paths.task_path("todo", doing_task.slug)
            if todo_path.exists():
                todo_task = parse_task_file(todo_path)
                human_task = self._create_needs_human_task(
                    todo_task,
                    result_payload,
                    log_path=log_path,
                    session_id=session_id,
                    export_path=export_path,
                )
                blocked_task = self._block_task_on_dependency(
                    todo_task, human_task.slug
                )
                self._write_task_file(blocked_task)
                self.git.commit_all_if_needed(
                    MSG_ESCALATE_HUMAN.format(slug=doing_task.slug)
                )
                active_attempt = self.state_store.load().active_attempt
                self.timeline.record(
                    TimelineEvent(
                        ts=TimelineStore.now_iso(),
                        event="task_escalated",
                        task=doing_task.slug,
                        detail={
                            "attempt": (
                                active_attempt.number
                                if active_attempt is not None
                                else None
                            ),
                            "blocker": (
                                result_payload.blocker
                                if result_payload is not None
                                else None
                            ),
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
                task_slug=doing_task.slug,
                phase="recover-needs-human-task",
                error=recovery_error,
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
                return state.attempts[-1]
            raise JriError("no task attempts recorded")
        matches = [attempt for attempt in state.attempts if attempt.task_slug == slug]
        if state.active_attempt is not None and state.active_attempt.task_slug == slug:
            matches.append(state.active_attempt)
        if not matches:
            matches = self._load_attempt_history(slug)
        if not matches:
            raise JriError(f"task '{slug}' has no recorded attempts")
        return max(matches, key=lambda attempt: attempt.number)

    def _inspect_log_path(self, attempt: AttemptState) -> Path:
        if attempt.log_path is not None:
            log_path = Path(attempt.log_path)
            if log_path.exists():
                return log_path
        return self._recover_missing_inspect_log(attempt)

    def _recover_missing_inspect_log(self, attempt: AttemptState) -> Path:
        log_path = self.paths.ralph_log_path(
            f"{attempt.task_slug}-inspect-recovered",
            int(time.time()),
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "\n".join(
                (
                    "JRI recovered missing inspect log.",
                    f"Task: {attempt.task_slug}",
                    f"Attempt: {attempt.number}",
                    f"Result: {attempt.result or 'unknown'}",
                    f"Original log path: {attempt.log_path or 'not recorded'}",
                )
            )
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
                detail={
                    "reason": "inspect_log_missing",
                    "attempt": attempt.number,
                    "log_path": str(log_path),
                },
            )
        )
        return log_path

    def _save_recovered_inspect_attempt(self, attempt: AttemptState) -> None:
        state = self.state_store.load()
        attempts = [
            attempt if self._same_attempt(existing, attempt) else existing
            for existing in state.attempts
        ]
        already_recorded = any(
            self._same_attempt(existing, attempt) for existing in state.attempts
        )
        if not already_recorded:
            attempts.append(attempt)
        active_attempt = state.active_attempt
        if active_attempt is not None and self._same_attempt(active_attempt, attempt):
            active_attempt = attempt
        self.state_store.save(
            replace(state, active_attempt=active_attempt, attempts=attempts)
        )
        self._persist_attempt_history(attempt)

    def _same_attempt(self, left: AttemptState, right: AttemptState) -> bool:
        return left.task_slug == right.task_slug and left.number == right.number

    def _load_attempt_history(self, slug: str) -> list[AttemptState]:
        history_path = self.paths.attempt_history_path(slug)
        if not history_path.exists():
            return []
        payload = json.loads(history_path.read_text(encoding="utf-8"))
        attempts = payload.get("attempts") if isinstance(payload, dict) else None
        if not isinstance(attempts, list):
            return []
        return [
            AttemptState.from_payload(item)
            for item in attempts
            if isinstance(item, dict)
        ]

    def _save_runtime_process(
        self, *, child_pid: int | None, task_log_path: Path
    ) -> None:
        state = self.state_store.load()
        process = state.process
        tracked_log_path: Path | None = task_log_path
        detached = False
        if process is not None and process.loop_pid == os.getpid():
            detached = process.detached
            if process.log_path:
                tracked_log_path = Path(process.log_path)
        self.state_store.save_process(
            loop_pid=os.getpid(),
            child_pid=child_pid,
            log_path=tracked_log_path,
            detached=detached,
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
                renderer = SavedLogRenderer(
                    cwd_hint=str(self.root).rstrip("/") + "/",
                )
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
                    process_exited = (
                        loop_process is not None and loop_process.poll() is not None
                    )
                    if (
                        process_exited
                        or loop_pid is None
                        or not self._is_pid_alive(loop_pid)
                    ):
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
    def _follow_control_monitor(self, *, enabled: bool) -> Iterator[_FollowControls]:
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
        if (
            state.active_attempt is not None
            and state.active_attempt.finished_at is None
        ):
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
                priority=(
                    human_task.priority
                    if human_task.priority is not None
                    else original_task.metadata.priority
                ),
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
        """Save a diff artifact between the previous end tag and current state.

        Finds the most recent end tag (jri/end/{slug}) and creates a diff
        between that tag and HEAD.
        """
        # Diff from the begin tag of current task to HEAD
        begin_tag = tag_name(task_slug, "begin")
        diff_text = self.git.diff(begin_tag, "HEAD")
        diff_path = self.paths.diff_artifact_path(task_slug)
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(diff_text, encoding="utf-8")

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
            "\n".join(
                (
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


def _load_managed_template(name: str) -> str:
    return (
        files("jri.core.template")
        .joinpath(*_template_resource_parts(name))
        .read_text(encoding="utf-8")
    )


def _template_resource_parts(name: str) -> tuple[str, ...]:
    parts = Path(name).parts
    if parts and parts[0] == ".jri":
        return parts[1:]
    return parts


def _single_line(text: str, *, limit: int) -> str:
    line = " ".join(text.split())
    if len(line) <= limit:
        return line
    return line[: limit - 3].rstrip() + "..."
