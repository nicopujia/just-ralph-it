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
from .git import (
    MSG_ESCALATE_FAILED,
    MSG_ESCALATE_HUMAN,
    MSG_PROMOTE,
    MSG_RALPH_FINALIZE,
    MSG_RALPH_PARTIAL,
    MSG_RECOVER_FAILED,
    MSG_RECOVER_NEEDS_HUMAN,
    MSG_RECOVER_STALE,
    MSG_START_BEGIN,
    MSG_START_COMPLETE,
    MSG_UPGRADE_AUTO,
    GitRepo,
    parse_tag_name,
    tag_name,
)
from .metrics import MetricEntry, MetricsStore
from .models import (
    AttemptState,
    Outcome,
    ProcessState,
    PromotionRecord,
    State,
    Task,
    TaskMetadata,
)
from .opencode import OpenCodeClient, OpenCodeServer
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
from .ui import task_footer, task_header

_INIT_COMMIT_PATHS = (
    ".jri",
    ".gitignore",
    "opencode.json",
)
_UPGRADE_COMMIT_PATHS = (
    ".jri/.gitignore",
    ".gitignore",
    "opencode.json",
)
_MANAGED_AGENT_FILENAMES = ("interrogator.md", "ralph.md")
_MANAGED_AGENT_PATHS = tuple(
    f".opencode/agents/{name}" for name in _MANAGED_AGENT_FILENAMES
)
_MANAGED_TOOL_FILENAMES = ("ralph-result.js",)
_MANAGED_TOOL_PATHS = tuple(
    f".opencode/tools/{name}" for name in _MANAGED_TOOL_FILENAMES
)
_MANAGED_CONFIG_FILENAMES = ("opencode.json",)
_TRACKED_TASK_DIRS = ("draft", "todo", "doing", "done")
_MAX_TASK_TITLE_LENGTH = 50
_MAX_FAILED_ATTEMPTS = 3


class JriService:
    def __init__(
        self,
        root: Path,
        *,
        opencode_client: OpenCodeClient | None = None,
        opencode_server: OpenCodeServer | None = None,
    ) -> None:
        self.root = root.resolve()
        self.paths = JriPaths(self.root)
        self.git = GitRepo(self.root)
        self.state_store = StateStore(self.paths.state_path)
        self.timeline = TimelineStore(self.paths.timeline_path)
        self.metrics = MetricsStore(self.paths.metrics_path)
        # Tests inject opencode_client to bypass the server entirely.
        # Production code uses opencode_server for Ralph runs and the
        # client only for `chat`/`list_sessions`/`export_session`.
        self._client_injected = opencode_client is not None
        self.opencode_client = opencode_client or OpenCodeClient()
        self.opencode_server = opencode_server
        self._halt_requested = False

    def init(self, *, force: bool, commit_message: str) -> None:
        self.git.ensure_repo()

        # Check for existing directories
        jri_exists = self.paths.jri_dir.exists()
        opencode_exists = (self.root / ".opencode").exists()

        if jri_exists or opencode_exists:
            if force:
                # Force mode: remove existing directories without prompting
                if jri_exists:
                    shutil.rmtree(self.paths.jri_dir)
                if opencode_exists:
                    shutil.rmtree(self.root / ".opencode")
            else:
                # Interactive mode: ask user what to do
                existing = []
                if jri_exists:
                    existing.append(".jri/")
                if opencode_exists:
                    existing.append(".opencode/")

                existing_str = " and ".join(existing)
                print(f"Existing {existing_str} directories found.")
                print("  [o] Overwrite - remove existing and reinitialize")
                print("  [a] Abort - cancel initialization")
                print("Choice [o/a]: ", end="", flush=True)

                try:
                    choice = input().strip().lower()
                except EOFError:
                    # Non-interactive environment (e.g., CI)
                    choice = "a"

                if choice == "o":
                    # User chose to overwrite
                    if jri_exists:
                        shutil.rmtree(self.paths.jri_dir)
                    if opencode_exists:
                        shutil.rmtree(self.root / ".opencode")
                else:
                    # User chose abort or invalid input
                    raise JriError("initialization aborted by user")

        created_files = self._create_scaffold()
        commit_paths = list(_INIT_COMMIT_PATHS)
        commit_paths.extend(str(path.relative_to(self.root)) for path in created_files)
        # Handle .opencode/.gitignore separately since .opencode/ is ignored
        opencode_gitignore = str(
            (self.root / ".opencode" / ".gitignore").relative_to(self.root)
        )
        # Stage all paths first, then commit
        self.git.run("add", "-A", "--", *commit_paths)
        self.git.run("add", "-f", "--", opencode_gitignore)
        self.git.run(
            "commit", "-m", commit_message, "--", *commit_paths, opencode_gitignore
        )

    def upgrade(self, *, commit_message: str) -> None:
        self.ensure_initialized()
        self._write_managed_files()
        # Handle .opencode/.gitignore separately since .opencode/ is ignored
        opencode_gitignore = str(
            (self.root / ".opencode" / ".gitignore").relative_to(self.root)
        )
        self.git.run("add", "-f", "--", opencode_gitignore)
        # Check if there's anything to commit (just the managed paths, not agent files)
        commit_paths = list(_UPGRADE_COMMIT_PATHS) + [opencode_gitignore]
        if not self.git.status_short(*commit_paths):
            return
        # Stage regular paths
        self.git.run("add", "-A", "--", *_UPGRADE_COMMIT_PATHS)
        result = self.git.run(
            "commit", "-m", commit_message, "--", *commit_paths, check=False
        )
        if result.returncode != 0:
            raise JriError(
                result.stderr.strip() or f"failed to commit: {commit_message}"
            )

    def chat(self, extra_args: list[str], *, fresh: bool = False) -> int:
        self.ensure_initialized()
        self._auto_upgrade_if_needed()
        self._update_compaction_reserved()
        if fresh:
            self.state_store.save_session(None)
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
        max_tasks: int | None = None,
        detached: bool = False,
        model: str | None = None,
        task_timeout: int | None = None,
        force: bool = False,
    ) -> int:
        self.ensure_initialized()
        self._recover_stale_start_state(
            mode="detached" if detached else "foreground", force=force
        )
        if detached:
            return self._start_detached(max_tasks, model, task_timeout)

        previous_model = self.opencode_client.model
        self.opencode_client.model = model
        previous_server_model: str | None = None
        if self.opencode_server is not None:
            previous_server_model = self.opencode_server.model
            self.opencode_server.model = model
        try:
            return self._run_loop(max_tasks, task_timeout=task_timeout, force=force)
        finally:
            self.opencode_client.model = previous_model
            if self.opencode_server is not None:
                self.opencode_server.model = previous_server_model

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

    def metrics_summary(self) -> str | None:
        """Return a human-readable metrics summary, or None if no metrics."""
        return self.metrics.summary()

    def promote_drafts(self, *, slugs: list[str]) -> list[Task]:
        self.ensure_initialized()

        draft_tasks = self._list_tasks("draft")
        selected = self._select_draft_tasks(draft_tasks, slugs)
        try:
            validate_draft_promotion(
                selected,
                all_draft_slugs={task.slug for task in draft_tasks},
                promoted_slugs=self._promoted_task_slugs(),
                promoted_deps=self._promoted_task_deps(),
            )
        except ValueError as exc:
            raise JriError(str(exc)) from exc

        promoted_tasks: list[Task] = []
        for task in selected:
            promoted_task = move_task(task, self.paths.task_dir("todo"))
            promoted_tasks.append(promoted_task)

        self.state_store.save_promotion(
            PromotionRecord(
                confirmed_at=int(time.time()),
                task_slugs=[task.slug for task in promoted_tasks],
            )
        )
        self.git.commit_paths_if_needed(
            MSG_PROMOTE,
            [
                self.git.relative_path(self.paths.task_dir("draft")),
                self.git.relative_path(self.paths.task_dir("todo")),
            ],
        )
        return promoted_tasks

    def reset(self, target_task: str | None = None) -> None:
        """Reset the repository to a specific tag.

        If target_task is provided, reset to jri/end/{target_task}.
        Otherwise, find the most recent end tag and reset to it.
        Falls back to jri/init for backward compatibility.
        """
        self.ensure_initialized()
        state = self.state_store.load()
        target_tag: str | None = None

        if target_task:
            # Reset to specific task's end tag
            target_tag = tag_name(target_task, "end")
            if not self.git.has_tag(target_tag):
                raise JriError(f"no end tag found for task '{target_task}'")
        else:
            # Find the most recent end tag
            target_tag = self._find_latest_end_tag()
            if target_tag is None:
                # Fall back to jri/init for backward compatibility
                if self.git.has_tag("jri/init"):
                    target_tag = "jri/init"
                else:
                    raise JriError("no task tag found — run `jri start` first")

        default = self.git.default_branch(hint=state.branch)
        current = self.git.current_branch()
        if current != default:
            self.git.run("checkout", "-f", default)
        self.git.reset_hard(target_tag)
        # Clean up worktree and ralph branch
        if self.paths.worktree_dir.exists():
            self.git.remove_worktree(self.paths.worktree_dir)
        if self.git.has_local_branch("ralph"):
            self.git.delete_branch("ralph")
        # Also clean up any legacy ralph/* branches
        branches = (
            self.git.run("branch", "--format=%(refname:short)")
            .stdout.strip()
            .splitlines()
        )
        for branch in branches:
            if branch.startswith("ralph/"):
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

    def _find_latest_end_tag(self) -> str | None:
        """Find the most recent end tag (jri/end/{slug}).

        Returns the tag name or None if no end tags exist.
        Uses git for-each-ref to list tags in reverse chronological order.
        """
        result = self.git.run(
            "for-each-ref",
            "--sort=-creatordate",
            "--format=%(refname:short)",
            "refs/tags/jri/end/",
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        tags = result.stdout.strip().split("\n")
        for tag in tags:
            parsed = parse_tag_name(tag)
            if parsed and parsed[0] == "end":
                return tag
        return None

    def ensure_initialized(self) -> None:
        self.git.ensure_repo()
        if not self.paths.jri_dir.exists():
            raise JriError("project is not initialized; run `jri init`")

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

    _GITIGNORE_CONTENT = "logs/\nsignals/\n*state.json*\nmetrics.json\nworktree/\n"

    def needs_upgrade(self) -> bool:
        """Check if managed files are outdated."""
        if not self.paths.gitignore_path.exists():
            return True
        current = self.paths.gitignore_path.read_text(encoding="utf-8")
        if current != self._GITIGNORE_CONTENT:
            return True
        for name in _MANAGED_AGENT_FILENAMES:
            path = self.paths.opencode_agents_dir / name
            if not path.exists():
                return True
            if path.read_text(encoding="utf-8") != _load_prompt(name):
                return True
        tool_dir = self.root / ".opencode" / "tools"
        for name in _MANAGED_TOOL_FILENAMES:
            path = tool_dir / name
            if not path.exists():
                return True
            if path.read_text(encoding="utf-8") != _load_prompt(name):
                return True
        return False

    def _auto_upgrade_if_needed(self) -> None:
        if self.needs_upgrade():
            self.upgrade(commit_message=MSG_UPGRADE_AUTO)

    def _write_managed_files(self) -> None:
        self.paths.gitignore_path.write_text(
            self._GITIGNORE_CONTENT,
            encoding="utf-8",
        )
        # Ensure root .gitignore exists with .opencode/ ignore
        # Note: we don't commit .opencode/.gitignore separately - it's inside .opencode/
        # which is ignored, so we use force-add when committing
        _ensure_ignore_entries(
            self.paths.root_gitignore_path,
            (".opencode/",),
        )
        # Ensure .opencode directory exists before writing its .gitignore
        opencode_dir = self.root / ".opencode"
        opencode_dir.mkdir(parents=True, exist_ok=True)
        # Write managed file entries to .opencode/.gitignore
        opencode_gitignore = opencode_dir / ".gitignore"
        _ensure_ignore_entries(
            opencode_gitignore,
            (*_MANAGED_AGENT_PATHS, *_MANAGED_TOOL_PATHS),
        )
        self.paths.opencode_agents_dir.mkdir(parents=True, exist_ok=True)
        for name in _MANAGED_AGENT_FILENAMES:
            (self.paths.opencode_agents_dir / name).write_text(
                _load_prompt(name),
                encoding="utf-8",
            )
        tool_dir = self.root / ".opencode" / "tools"
        tool_dir.mkdir(parents=True, exist_ok=True)
        for name in _MANAGED_TOOL_FILENAMES:
            (tool_dir / name).write_text(
                _load_prompt(name),
                encoding="utf-8",
            )
        for name in _MANAGED_CONFIG_FILENAMES:
            (self.root / name).write_text(
                _load_prompt(name),
                encoding="utf-8",
            )

    def _update_compaction_reserved(self) -> None:
        """Update opencode.json reserved tokens based on the model context window."""
        config_path = self.root / "opencode.json"
        if not config_path.exists():
            return
        try:
            import json

            config = json.loads(config_path.read_text(encoding="utf-8"))
            reserved = _compute_reserved()
            if reserved is not None and isinstance(config.get("compaction"), dict):
                config["compaction"]["reserved"] = reserved
                config_path.write_text(
                    json.dumps(config, indent=2) + "\n", encoding="utf-8"
                )
        except Exception:
            pass  # Non-critical — fall back to template default

    def _start_detached(
        self, max_tasks: int | None, model: str | None, task_timeout: int | None
    ) -> int:
        state = self.state_store.load()
        if state.process and state.process.loop_pid:
            raise JriError("a Ralph process is already tracked")

        command = [sys.executable, "-m", "jri", "start"]
        if max_tasks is not None:
            command.extend(["-n", str(max_tasks)])
        if model is not None:
            command.extend(["--model", model])
        if task_timeout is not None:
            command.extend(["--task-timeout", str(task_timeout)])

        log_path = self.paths.ralph_log_path("detached", int(time.time()))
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

    def _run_loop(
        self,
        max_tasks: int | None,
        task_timeout: int | None = None,
        force: bool = False,
    ) -> int:
        try:
            doing = list_tasks(self.paths.task_dir("doing"), git_repo=self.git)
        except ValueError as exc:
            raise JriError(str(exc)) from exc
        if doing:
            raise JriError("a task is already in progress")
        self._auto_upgrade_if_needed()
        self._handle_dirty_workdir(force=force)
        self._handle_wrong_branch(force=force)
        self._ensure_initial_tag()
        if self.paths.stop_signal_path.exists():
            self.paths.stop_signal_path.unlink()

        completed = 0
        failed_slugs: set[str] = set()
        self._halt_requested = False
        old_handlers = self._install_signal_handlers()
        # Start the opencode server unless tests injected a fake client.
        server_started_here = False
        if not self._client_injected:
            if self.opencode_server is None:
                self.opencode_server = OpenCodeServer(model=self.opencode_client.model)
            outcome_path = self.paths.jri_dir / "signals" / "result"
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            # Ensure the worktree exists before the server starts so Ralph's
            # tools resolve paths against the worktree, not the main repo.
            wt_git, _ = self._ensure_worktree()
            self._sync_worktree(wt_git)
            self.opencode_server.start(
                env={"JRI_OUTCOME_PATH": str(outcome_path)},
                cwd=self.paths.worktree_dir,
            )
            server_started_here = True
        try:
            while max_tasks is None or completed < max_tasks:
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

                # Check if we've reached the task limit before starting
                if max_tasks is not None and completed >= max_tasks:
                    break

                outcome = self._run_task(next_task, task_timeout=task_timeout)
                if outcome == "completed":
                    completed += 1
                elif outcome == "failed":
                    failed_slugs.add(next_task.slug)
                    if (
                        self._count_failed_attempts(next_task.slug)
                        >= _MAX_FAILED_ATTEMPTS
                    ):
                        self._escalate_failed_task(next_task)
                elif outcome == "needs human":
                    failed_slugs.add(next_task.slug)
                elif outcome == "timeout":
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
            if max_tasks is not None and completed >= max_tasks:
                self.timeline.record(
                    TimelineEvent(
                        ts=TimelineStore.now_iso(),
                        event="loop_stopped",
                        task=None,
                        detail={"reason": "task_limit", "limit": max_tasks},
                    )
                )
        finally:
            if server_started_here and self.opencode_server is not None:
                self.opencode_server.stop()
            self._restore_signal_handlers(old_handlers)
            self.state_store.clear_process()

        return completed

    def _ensure_worktree(self) -> tuple[GitRepo, JriPaths]:
        """Ensure the persistent ``ralph`` worktree exists and return helpers."""
        wt_dir = self.paths.worktree_dir
        branch = "ralph"

        if not self.git.has_local_branch(branch):
            default_ref = self.git.rev_parse(self._default_branch())
            self.git.run("branch", branch, default_ref)

        if not wt_dir.exists():
            self.git.add_worktree(wt_dir, branch)

        return GitRepo(wt_dir), JriPaths(wt_dir)

    def _sync_worktree(self, wt_git: GitRepo) -> None:
        """Reset the ``ralph`` branch to the default-branch tip."""
        default_ref = self.git.rev_parse(self._default_branch())
        self.git.reset_branch("ralph", default_ref)
        wt_git.run("checkout", "--force", "ralph")
        wt_git.run("clean", "-fd")
        self._copy_gitignored_files_to_worktree()

    def _copy_gitignored_files_to_worktree(self) -> None:
        """Copy gitignored managed files into the worktree."""
        wt = self.paths.worktree_dir
        for src_dir, filenames in (
            (".opencode/agents", _MANAGED_AGENT_FILENAMES),
            (".opencode/tools", _MANAGED_TOOL_FILENAMES),
        ):
            dst_dir = wt / src_dir
            dst_dir.mkdir(parents=True, exist_ok=True)
            for name in filenames:
                src = self.root / src_dir / name
                if src.exists():
                    shutil.copy2(src, dst_dir / name)
        # Remove any plugin dir left over from earlier versions — the
        # pruning plugin must NOT exist in Ralph's worktree.
        wt_plugin_dir = wt / ".opencode" / "plugin"
        if wt_plugin_dir.exists():
            shutil.rmtree(wt_plugin_dir)
        # Copy opencode.json
        src = self.root / "opencode.json"
        if src.exists():
            shutil.copy2(src, wt / "opencode.json")

    def _run_task(self, task: Task, task_timeout: int | None = None) -> Outcome:
        state = self.state_store.load()
        started_at = int(time.time())
        log_path = self.paths.ralph_log_path(task.slug, started_at)
        branch = "ralph"
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
        self.state_store.save_process(
            loop_pid=os.getpid(),
            child_pid=None,
            log_path=log_path,
            detached=False,
        )

        prompt_text = (
            f"Solve `{doing_task.path.relative_to(wt_paths.root)}`. Commit frequently."
        )
        on_start_cb = lambda child_pid: self.state_store.save_process(  # noqa: E731
            loop_pid=os.getpid(),
            child_pid=child_pid,
            log_path=log_path,
            detached=False,
        )
        if self.opencode_server is not None and not self._client_injected:
            outcome_path = self.paths.jri_dir / "signals" / "result"
            result = self.opencode_server.run_ralph_task(
                root=wt_paths.root,
                prompt=prompt_text,
                log_path=log_path,
                outcome_path=outcome_path,
                on_start=on_start_cb,
                timeout=task_timeout,
            )
        else:
            result = self.opencode_client.run_ralph_task(
                root=wt_paths.root,
                prompt=prompt_text,
                log_path=log_path,
                on_start=on_start_cb,
                timeout=task_timeout,
            )

        # Check for task timeout
        finished_at = int(time.time())
        if deadline is not None and finished_at > deadline:
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
            self._finish_attempt(attempt, outcome="failed")
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

        # Record any warnings from the OpenCode run (e.g., missing outcome marker)
        for warning in result.warnings:
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="stderr_warning",
                    task=task.slug,
                    detail={"message": warning},
                )
            )

        if result.returncode != 0:
            self._recover_failed_task_wt(doing_task, wt_git)
            self._finish_attempt(attempt, outcome="failed")
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
            raise JriError(f"OpenCode exited with status {result.returncode}")

        export_path = self._export_session_if_available(
            result.session_id,
            task_slug=task.slug,
        )
        attempt = replace(attempt, session_id=result.session_id)
        self.state_store.save_active_attempt(attempt)

        # Cleanup session after export (only for OpenCodeServer, not fake clients)
        if (
            self.opencode_server is not None
            and not self._client_injected
            and result.session_id is not None
        ):
            self.opencode_server._delete_session(result.session_id)

        if not doing_task.path.exists():
            self._recover_failed_task_wt(doing_task, wt_git)
            self._finish_attempt(attempt, outcome="failed")
            relative_path = doing_task.path.relative_to(wt_paths.root)
            raise JriError(f"task file `{relative_path}` disappeared during Ralph run")

        # If a project tool (like prettier) modified the task file in
        # place, restore it to baseline rather than failing the whole
        # task. Ralph's actual work is still valid.
        if doing_task.path.read_text(encoding="utf-8") != doing_task_baseline:
            doing_task.path.write_text(doing_task_baseline, encoding="utf-8")

        if result.outcome == "needs human":
            self._recover_needs_human_task(
                doing_task,
                branch,
                log_path=log_path,
                session_id=result.session_id,
                export_path=export_path,
            )
            self._finish_attempt(attempt, outcome="needs human")
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="task_needs_human",
                    task=task.slug,
                )
            )
            print(task_footer("needs human"))
            sys.stdout.flush()
            return "needs human"

        if result.outcome == "failed":
            self._recover_failed_task_wt(doing_task, wt_git)
            self._finish_attempt(attempt, outcome="failed")
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="task_failed",
                    task=task.slug,
                    detail={"reason": "ralph_outcome_failed"},
                )
            )
            print(task_footer("failed"))
            sys.stdout.flush()
            return "failed"

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
                self._finish_attempt(attempt, outcome="failed")
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
                self._finish_attempt(attempt, outcome="failed")
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

        # Merge worktree branch into default
        self.git.merge_ff_only(branch)

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

        finished_at = int(time.time())
        attempt = replace(attempt, finished_at=finished_at, outcome="completed")
        self.state_store.save_active_attempt(attempt)
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

    def _ensure_initial_tag(self) -> None:
        if not self.git.has_tag("jri/init"):
            self.git.create_tag("jri/init")

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
        sys.stdout.write("Switch to main? [Y/n] ")
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
            raise JriError("a Ralph process is already running")

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
                if self._attempt_completion_applied(active_attempt):
                    self._record_recovery(
                        mode=mode,
                        reason="resume-completed-attempt",
                        task_slug=doing_tasks[0].slug,
                        process=process,
                    )
                    self._complete_attempt(active_attempt, doing_task=doing_tasks[0])
                    return
            self._recover_stale_task(
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
            elif current_branch == "ralph":
                self.git.commit_all_if_needed(
                    MSG_RALPH_PARTIAL.format(slug=doing_task.slug)
                )
                self.git.checkout(default)
            else:
                # Legacy ralph/{slug} branch support
                expected = f"ralph/{doing_task.slug}"
                if current_branch == expected:
                    self.git.commit_all_if_needed(
                        MSG_RALPH_PARTIAL.format(slug=doing_task.slug)
                    )
                    self.git.checkout(default)
                    self.git.delete_branch(expected)
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
        branch_ok = attempt.branch == "ralph"
        return attempt.task_slug == task.slug and branch_ok

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
                    MSG_RALPH_PARTIAL.format(slug=attempt.task_slug)
                )
            self.git.checkout(default)
        elif current_branch != default:
            raise JriError(f"jri start must begin from the {default} branch")

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
        self.state_store.save_active_attempt(
            replace(attempt, finished_at=finished_at, outcome="completed")
        )
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
        self.timeline.record(
            TimelineEvent(
                ts=TimelineStore.now_iso(),
                event="recovery_completed",
                task=task_slug,
                detail={"mode": mode, "reason": reason, "message": line},
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
        return self.paths.ralph_log_path("unknown", 0)

    def _last_attempt_session_id(self, task_slug: str) -> str | None:
        state = self.state_store.load()
        for attempt in reversed(state.attempts):
            if attempt.task_slug == task_slug:
                return attempt.session_id
        return None

    def _escalate_failed_task(self, task: Task) -> None:
        self.timeline.record(
            TimelineEvent(
                ts=TimelineStore.now_iso(),
                event="task_escalated",
                task=task.slug,
                detail={"failed_attempts": self._count_failed_attempts(task.slug)},
            )
        )
        todo_path = self.paths.task_path("todo", task.slug)
        if not todo_path.exists():
            return
        todo_task = parse_task_file(todo_path)
        log_path = self._last_attempt_log_path(todo_task.slug)
        session_id = self._last_attempt_session_id(todo_task.slug)
        export_path = self._export_session_if_available(
            session_id,
            task_slug=task.slug,
        )
        human_task = self._create_needs_human_task(
            todo_task,
            log_path=log_path,
            session_id=session_id,
            export_path=export_path,
        )
        blocked_task = self._block_task_on_dependency(todo_task, human_task.slug)
        self._write_task_file(blocked_task)
        self.git.commit_all_if_needed(
            MSG_ESCALATE_FAILED.format(slug=task.slug, n=_MAX_FAILED_ATTEMPTS)
        )

    def _recover_needs_human_task(
        self,
        doing_task: Task,
        branch: str,
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

    def _export_session_if_available(
        self,
        session_id: str | None,
        *,
        task_slug: str | None = None,
    ) -> Path | None:
        if session_id is None:
            return None
        export_path = self.paths.external_opencode_dir / f"{session_id}.json"
        try:
            self.opencode_client.export_session(session_id, export_path)
        except JriError as exc:
            # Surface export failure to timeline and stderr
            error_msg = f"Failed to export session {session_id}: {exc}"
            print(error_msg, file=sys.stderr)
            self.timeline.record(
                TimelineEvent(
                    ts=TimelineStore.now_iso(),
                    event="export_failed",
                    task=task_slug,
                    detail={
                        "session_id": session_id,
                        "error": str(exc),
                    },
                )
            )
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


_MODELS_DEV_URL = "https://models.dev/api.json"
_MAX_RESERVED_TOKENS = 500_000
_RESERVED_RATIO = 0.4


def _compute_reserved(model: str | None = None) -> int | None:
    """Compute compaction reserved tokens from the model context window.

    Fetches context-window size from models.dev and returns
    ``min(0.4 * context, 500_000)``.  Returns ``None`` on any failure.
    """
    import json
    import urllib.request

    try:
        req = urllib.request.Request(_MODELS_DEV_URL, headers={"User-Agent": "jri/0.1"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            registry = json.loads(resp.read())
    except Exception:
        return None

    # Search all providers for the model
    target = model or "claude-sonnet-4-20250514"
    for provider in registry.values():
        if not isinstance(provider, dict):
            continue
        models = provider.get("models")
        if not isinstance(models, dict):
            continue
        entry = models.get(target)
        if isinstance(entry, dict):
            limit = entry.get("limit")
            if isinstance(limit, dict):
                context = limit.get("context")
                if isinstance(context, int) and context > 0:
                    return min(int(context * _RESERVED_RATIO), _MAX_RESERVED_TOKENS)
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
