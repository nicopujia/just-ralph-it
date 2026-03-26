"""Ralph autonomous coding loop — picks issues and solves them one at a time."""

import asyncio
import collections
import json
import logging
import os
from pathlib import Path
from typing import Optional

from app import tasks
from app.database import get_db

from app.prompts.ralph import RALPH_SYSTEM_PROMPT
from app.sse_bus import sse_bus

logger = logging.getLogger(__name__)

STDOUT_BUFFER_SIZE = 5000


def build_ralph_prompt(issue: dict, user_name: str, user_email: str) -> str:
    """Build the prompt that Ralph receives for a single issue."""
    issue_id = issue.get("id", "")
    title = issue.get("title", "")
    issue_type = issue.get("issue_type", "")
    priority = issue.get("priority", "")
    description = issue.get("description", "")
    acceptance_criteria = issue.get("acceptance_criteria", "")
    design = issue.get("design") or "N/A"
    notes = issue.get("notes") or "N/A"

    return (
        f"Read README.md in the project root and any relevant subdirectories.\n"
        f"Then read this issue:\n"
        f"\n"
        f"Issue: {issue_id}\n"
        f"Title: {title}\n"
        f"Type: {issue_type}\n"
        f"Priority: {priority}\n"
        f"\n"
        f"Description:\n"
        f"{description}\n"
        f"\n"
        f"Acceptance Criteria:\n"
        f"{acceptance_criteria}\n"
        f"\n"
        f"Design:\n"
        f"{design}\n"
        f"\n"
        f"Notes:\n"
        f"{notes}\n"
        f"\n"
        f"Solve this issue completely. Follow TDD: write tests from acceptance criteria first, then implement.\n"
        f'When done: git add -A && git commit -m "<msg>" '
        f'--trailer "Co-authored-by: {user_name} <{user_email}>"\n'
        f"Then: mv .jri/tasks/doing/{issue_id}.md .jri/tasks/done/"
    )


class RalphLoop:
    """Manages the Ralph autonomous loop for a single project."""

    def __init__(
        self,
        project_id: int,
        project_dir: str,
        project_name: str,
        user_github_name: str,
        user_github_email: str,
        task_budget: int = 0,
    ) -> None:
        self.project_id = project_id
        self.project_dir = project_dir
        self.project_name = project_name
        self.status: str = "stopped"
        self.current_issue_id: Optional[str] = None
        self.iteration: int = 0
        self.process: Optional[asyncio.subprocess.Process] = None
        self.stdout_lines: collections.deque = collections.deque(maxlen=STDOUT_BUFFER_SIZE)
        self.user_github_name = user_github_name
        self.user_github_email = user_github_email
        self._subscribers: set[asyncio.Queue] = set()
        self._task: Optional[asyncio.Task] = None
        self.task_budget: int = task_budget
        self.tasks_completed: int = 0
        self._payment_event: asyncio.Event = asyncio.Event()

    @staticmethod
    async def check_interrupted(project_dir: str, project_name: str) -> None:
        """Check if a previous loop was interrupted and clean up."""
        state_path = Path(project_dir) / ".jri" / "state.json"
        # Also check legacy path
        legacy_path = Path(project_dir) / ".jri_state"
        if legacy_path.exists() and not state_path.exists():
            state_path = legacy_path
        if not state_path.exists():
            return
        try:
            state = json.loads(state_path.read_text())
            if state.get("status") == "running":
                issue_id = state.get("current_issue_id")
                logger.warning(
                    "Found interrupted Ralph loop for %s on issue %s, recovering",
                    project_name, issue_id,
                )
                # git reset --hard HEAD
                reset_proc = await asyncio.create_subprocess_exec(
                    "git", "reset", "--hard", "HEAD",
                    cwd=project_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await reset_proc.communicate()
                # Clean up abandoned worktrees
                wt_proc = await asyncio.create_subprocess_exec(
                    "git", "worktree", "prune",
                    cwd=project_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await wt_proc.communicate()
                # Reopen the issue
                if issue_id:
                    try:
                        tasks.set_status(project_dir, issue_id, "todo")
                    except Exception:
                        logger.warning("Could not reopen issue %s", issue_id)
                    await sse_bus.publish(
                        project_name, "issue_update",
                        {"issue_id": issue_id, "action": "reopened"},
                    )
                # Clean up state file(s)
                state_path.unlink(missing_ok=True)
                legacy_path.unlink(missing_ok=True)
        except Exception:
            logger.exception("Failed to recover interrupted loop for %s", project_name)

    async def start(self) -> None:
        """Set status to running and kick off the loop task."""
        await self.check_interrupted(self.project_dir, self.project_name)
        self.status = "running"
        await self._update_db_status("running")
        self._task = asyncio.create_task(self._loop())

    async def _poll_for_human_blockers(self) -> None:
        """Check for issues assigned to Human and create notifications."""
        try:
            all_issues = tasks.list_all(self.project_dir)

            human_issues = [
                i for i in all_issues
                if i.get("assignee") == "Human" and i.get("status") == "todo"
            ]

            if not human_issues:
                return

            async with get_db() as db:
                for issue in human_issues:
                    issue_id = issue.get("id", "")
                    title = issue.get("title", "")

                    # Check if notification already exists for this issue
                    cursor = await db.execute(
                        "SELECT id FROM notifications "
                        "WHERE project_id = ? AND task_id = ?",
                        (self.project_id, issue_id),
                    )
                    existing = await cursor.fetchone()
                    if existing:
                        continue

                    message = f"Ralph needs help: {title}"
                    cursor = await db.execute(
                        "INSERT INTO notifications (project_id, message, task_id) "
                        "VALUES (?, ?, ?)",
                        (self.project_id, message, issue_id),
                    )
                    notification_id = cursor.lastrowid
                    await db.commit()

                    # Get created_at for the SSE event
                    cursor = await db.execute(
                        "SELECT created_at FROM notifications WHERE id = ?",
                        (notification_id,),
                    )
                    row = await cursor.fetchone()
                    created_at = row["created_at"] if row else ""

                    await sse_bus.publish(
                        self.project_name, "notification",
                        {
                            "id": notification_id,
                            "message": message,
                            "task_id": issue_id,
                            "created_at": created_at,
                        },
                    )

        except Exception:
            logger.exception("Error polling for human blockers in project %s", self.project_name)

    async def _loop(self) -> None:
        """Core Ralph loop: pick issue, solve, push, repeat."""
        try:
            while self.status == "running":
                # --- Poll for human-assigned blockers ---
                await self._poll_for_human_blockers()

                # --- Get ready issues ---
                ready_issues = tasks.get_ready(self.project_dir)[:1]

                logger.info("Project %s: found %d ready issues", self.project_name, len(ready_issues))

                if not ready_issues:
                    self.status = "stopped"
                    self._save_state()
                    await self._update_db_status("idle")

                    # --- Deploy if configured ---
                    await self._deploy_if_configured()

                    await sse_bus.publish(
                        self.project_name, "ralph_status",
                        {"status": "idle", "message": "No more ready issues"},
                    )
                    break

                # --- Budget enforcement ---
                if self.task_budget > 0 and self.tasks_completed >= self.task_budget:
                    # Count remaining todo tasks
                    all_ready = tasks.get_ready(self.project_dir)
                    unpaid_count = len(all_ready)

                    if unpaid_count > 0:
                        logger.info(
                            "Project %s: budget exhausted (%d/%d), %d unpaid tasks remain",
                            self.project_name, self.tasks_completed,
                            self.task_budget, unpaid_count,
                        )
                        self.status = "payment_required"
                        self._save_state()
                        await self._update_db_status("payment_required")
                        await sse_bus.publish(
                            self.project_name, "payment_required",
                            {"unpaid_count": unpaid_count, "project_name": self.project_name},
                        )
                        await sse_bus.publish(
                            self.project_name, "ralph_status",
                            {"status": "payment_required", "unpaid_count": unpaid_count},
                        )

                        # Wait for payment to resume (or stop)
                        self._payment_event.clear()
                        await self._payment_event.wait()

                        # If stop() was called while waiting, exit
                        if self.status == "stopping":
                            break

                        # After resume, reset counter and continue the loop
                        self.tasks_completed = 0
                        self.status = "running"
                        self._save_state()
                        await self._update_db_status("running")
                        logger.info(
                            "Project %s: resumed after payment, new budget=%d",
                            self.project_name, self.task_budget,
                        )
                        continue
                    # else: over budget but no more todo tasks, normal exit
                    # (will hit the "no ready_issues" check next iteration)

                issue = ready_issues[0]
                self.current_issue_id = issue.get("id", "")
                self.iteration += 1

                # --- Save state ---
                self._save_state()
                await self._update_db_issue()

                try:
                    # --- Claim ---
                    tasks.set_status(self.project_dir, self.current_issue_id, "doing")
                    tasks.update_field(
                        self.project_dir, self.current_issue_id,
                        assignee="ralph",
                    )
                    await sse_bus.publish(self.project_name, "issue_update", {"issue_id": self.current_issue_id, "action": "claimed"})

                    # --- Clear stdout for new issue ---
                    self.stdout_lines.clear()
                    await sse_bus.publish(self.project_name, "ralph_stdout_clear", {})

                    # --- Build prompt ---
                    prompt = build_ralph_prompt(
                        issue, self.user_github_name, self.user_github_email,
                    )

                    # --- Run Claude ---
                    logger.info("Project %s: starting Claude for issue %s (prompt: %d chars)", self.project_name, self.current_issue_id, len(prompt))
                    self.process = await asyncio.create_subprocess_exec(
                        "claude", "-p",
                        "--model", "opus",
                        "--output-format", "stream-json",
                        "--verbose",
                        "--dangerously-skip-permissions",
                        "--system-prompt", RALPH_SYSTEM_PROMPT,
                        "--allowedTools", "Bash Read Write Edit Glob Grep WebFetch WebSearch",
                        "--", prompt,
                        cwd=self.project_dir,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        env=self._env({}),
                    )

                    # Stream stdout
                    await self._stream_process_output()

                    # Wait for exit
                    await self.process.wait()
                    exit_code = self.process.returncode
                    logger.info("Project %s: Claude exited with code %d", self.project_name, exit_code)

                    if exit_code != 0:
                        await self._recover(self.current_issue_id)
                        continue

                    # --- Push ---
                    push_proc = await asyncio.create_subprocess_exec(
                        "git", "push",
                        cwd=self.project_dir,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    push_stdout, push_stderr = await push_proc.communicate()
                    if push_proc.returncode == 0:
                        logger.info("Pushed changes to GitHub for issue %s", self.current_issue_id)
                    else:
                        logger.warning(
                            "git push failed for issue %s (exit %d): %s",
                            self.current_issue_id,
                            push_proc.returncode,
                            push_stderr.decode(errors="replace").strip(),
                        )

                    # --- Check if issue was closed ---
                    issue_data = tasks.get_task(
                        self.project_dir, self.current_issue_id,
                    )
                    if issue_data and issue_data.get("status") != "done":
                        logger.warning(
                            "Issue %s was not closed by Ralph after iteration %d",
                            self.current_issue_id, self.iteration,
                        )

                    # Notify frontend of issue state change
                    await sse_bus.publish(self.project_name, "issue_update", {"issue_id": self.current_issue_id, "action": "completed"})
                    self.tasks_completed += 1

                except Exception:
                    logger.exception(
                        "Iteration %d crashed on issue %s in project %s",
                        self.iteration, self.current_issue_id, self.project_name,
                    )
                    # Check if there are unpushed commits (post-commit crash)
                    try:
                        unpushed = await asyncio.create_subprocess_exec(
                            "git", "log", "--oneline", "@{u}..HEAD",
                            cwd=self.project_dir,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        up_out, up_err = await unpushed.communicate()
                        has_unpushed = unpushed.returncode == 0 and up_out.strip()

                        if has_unpushed:
                            # Commits exist locally -- try to push them
                            push_proc = await asyncio.create_subprocess_exec(
                                "git", "push",
                                cwd=self.project_dir,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            await push_proc.communicate()
                            if push_proc.returncode == 0:
                                # Push succeeded, move task to done
                                logger.info(
                                    "Post-crash push succeeded for issue %s",
                                    self.current_issue_id,
                                )
                                try:
                                    tasks.set_status(
                                        self.project_dir,
                                        self.current_issue_id,
                                        "done",
                                    )
                                except Exception:
                                    logger.warning(
                                        "Could not move issue %s to done after post-crash push",
                                        self.current_issue_id,
                                    )
                                continue
                            else:
                                logger.warning(
                                    "Post-crash push failed for issue %s, recovering",
                                    self.current_issue_id,
                                )
                    except Exception:
                        logger.warning(
                            "Could not check unpushed commits for issue %s",
                            self.current_issue_id,
                        )

                    await self._recover(self.current_issue_id)
                    continue

        except Exception:
            logger.exception("Ralph loop crashed for project %s", self.project_name)
        finally:
            self.status = "stopped"
            self._save_state()
            await self._update_db_status("idle")

    async def _deploy_if_configured(self) -> None:
        """Deploy the project if deploy_type is configured in the DB."""
        try:
            async with get_db() as db:
                cursor = await db.execute(
                    "SELECT p.deploy_type, p.deploy_port, p.deploy_start_command, "
                    "p.deploy_subdomain, u.github_username "
                    "FROM projects p JOIN users u ON p.user_id = u.id "
                    "WHERE p.id = ?",
                    (self.project_id,),
                )
                row = await cursor.fetchone()

            if not row:
                return

            row_dict = dict(row)
            deploy_type = row_dict.get("deploy_type")
            if not deploy_type:
                return

            deploy_port = row_dict.get("deploy_port")
            deploy_start_command = row_dict.get("deploy_start_command")
            github_username = row_dict.get("github_username", "unknown").lower()
            deploy_subdomain = row_dict.get("deploy_subdomain") or f"{self.project_name.lower()}.{github_username}"
            # Ensure subdomain uses the new {project}.{username} format
            if "." not in deploy_subdomain:
                deploy_subdomain = f"{deploy_subdomain}.{github_username}"

            from app.deploy_manager import deploy_dynamic
            await deploy_dynamic(
                self.project_name, self.project_dir,
                deploy_start_command or "", deploy_port or 9000,
            )

            # Update deploy_status in DB
            async with get_db() as db:
                await db.execute(
                    "UPDATE projects SET deploy_status = 'running' WHERE id = ?",
                    (self.project_id,),
                )
                await db.commit()

            # Publish deployed SSE event
            await sse_bus.publish(
                self.project_name, "ralph_status",
                {
                    "status": "deployed",
                    "url": f"https://{deploy_subdomain}.justralph.it",
                },
            )
            logger.info(
                "Deployed project %s to https://%s.justralph.it",
                self.project_name, deploy_subdomain,
            )

        except Exception:
            logger.exception("Deployment failed for project %s", self.project_name)

    async def _recover(self, issue_id: str) -> None:
        """Reset git state, reopen issue, log crash, and publish event."""
        logger.warning(
            "Recovering from crash on issue %s in project %s",
            issue_id, self.project_name,
        )

        recovery_msg = f"Crashed on issue {issue_id}, recovering..."
        await sse_bus.publish(
            self.project_name, "ralph_stdout", {"line": recovery_msg},
        )

        # git reset --hard HEAD
        reset_proc = await asyncio.create_subprocess_exec(
            "git", "reset", "--hard", "HEAD",
            cwd=self.project_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await reset_proc.communicate()

        # Clean up abandoned worktrees
        wt_proc = await asyncio.create_subprocess_exec(
            "git", "worktree", "prune",
            cwd=self.project_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await wt_proc.communicate()

        # Reopen issue
        try:
            tasks.set_status(self.project_dir, issue_id, "todo")
        except Exception:
            logger.warning("Could not reopen issue %s during recovery", issue_id)

        await sse_bus.publish(
            self.project_name, "ralph_status",
            {"status": "crash_recovery", "issue_id": issue_id},
        )
        await sse_bus.publish(
            self.project_name, "issue_update",
            {"issue_id": issue_id, "action": "reopened"},
        )

        logger.info(
            "Recovery complete for issue %s in project %s",
            issue_id, self.project_name,
        )

    async def resume_after_payment(self, new_budget: int) -> None:
        """Resume the loop after payment with an updated budget."""
        self.task_budget = new_budget
        self._payment_event.set()

    async def stop(self) -> None:
        """Gracefully stop after the current iteration finishes."""
        if self.status not in ("running", "payment_required"):
            return
        self.status = "stopping"
        # Unblock if waiting for payment
        self._payment_event.set()
        # If a process is running, wait with timeout then kill
        if self.process and self.process.returncode is None:
            try:
                await asyncio.wait_for(self.process.wait(), timeout=30)
            except asyncio.TimeoutError:
                logger.warning("Claude process did not exit in 30s, killing it")
                self.process.kill()
                await self.process.wait()
            except Exception:
                pass
        # Wait for the task to finish with timeout
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                logger.warning("Ralph loop task did not finish in 10s, cancelling")
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            except Exception:
                pass
        self.status = "stopped"
        await self._update_db_status("idle")

    def subscribe(self) -> asyncio.Queue:
        """Create a new subscriber queue for stdout streaming."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        self._subscribers.discard(queue)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _stream_process_output(self) -> None:
        """Read lines from the subprocess stdout and fan out to subscribers."""
        assert self.process and self.process.stdout

        log_dir = Path(self.project_dir) / ".jri" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "ralph.log"
        log_file = open(log_path, "a", encoding="utf-8")

        try:
            while True:
                line_bytes = await self.process.stdout.readline()
                if not line_bytes:
                    break
                raw = line_bytes.decode(errors="replace").strip()
                if not raw:
                    continue

                # Try to parse stream-json and extract readable content
                display_line = self._parse_stream_line(raw)
                if not display_line:
                    # Log unparsed non-empty lines for debugging
                    if raw and not raw.startswith("{"):
                        logger.debug("Unparsed non-JSON line: %s", raw[:200])
                    continue

                self.stdout_lines.append(display_line)

                # Write to ralph log file
                log_file.write(display_line + "\n")
                log_file.flush()

                # Publish to local subscribers
                for q in self._subscribers.copy():
                    try:
                        q.put_nowait(display_line)
                    except asyncio.QueueFull:
                        pass

                # Publish to SSE bus
                await sse_bus.publish(
                    self.project_name, "ralph_stdout", {"line": display_line},
                )
        finally:
            log_file.close()


    def _parse_stream_line(self, raw: str) -> str | None:
        """Parse a stream-json line and return a human-readable string, or None to skip."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw  # Not JSON, show as-is

        msg_type = data.get("type")

        if msg_type == "assistant":
            content_blocks = data.get("message", {}).get("content", [])
            parts = []
            for block in content_blocks:
                if block.get("type") == "text":
                    parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    name = block.get("name", "")
                    inp = block.get("input", {})
                    if name == "Bash":
                        parts.append(f"$ {inp.get('command', '')}")
                    elif name == "Write":
                        parts.append(f"Writing {inp.get('file_path', '')}")
                    elif name == "Edit":
                        parts.append(f"Editing {inp.get('file_path', '')}")
                    elif name == "Read":
                        parts.append(f"Reading {inp.get('file_path', '')}")
                    elif name == "Glob":
                        parts.append(f"Searching {inp.get('pattern', '')}")
                    elif name == "Grep":
                        parts.append(f"Grepping {inp.get('pattern', '')}")
                    else:
                        parts.append(f"[{name}]")
            return "\n".join(parts) if parts else None

        elif msg_type == "content_block_delta":
            delta = data.get("delta", {})
            if delta.get("type") == "text_delta":
                return delta.get("text", "")
            return None

        elif msg_type == "result":
            result = data.get("result", "")
            if result:
                return "--- Done ---"
            return None

        elif msg_type == "system":
            return None  # Skip system init messages

        return None  # Skip unknown types

    def _save_state(self) -> None:
        """Persist loop state to .jri/state.json using atomic write."""
        state = {
            "project_id": self.project_id,
            "status": self.status,
            "current_issue_id": self.current_issue_id,
            "iteration": self.iteration,
        }
        jri_dir = Path(self.project_dir) / ".jri"
        jri_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = jri_dir / "state.json.tmp"
        state_path = jri_dir / "state.json"
        tmp_path.write_text(json.dumps(state, indent=2))
        os.replace(tmp_path, state_path)

    async def _update_db_status(self, status: str) -> None:
        async with get_db() as db:
            await db.execute(
                "UPDATE projects SET ralph_loop_status = ? WHERE id = ?",
                (status, self.project_id),
            )
            await db.commit()

    async def _update_db_issue(self) -> None:
        async with get_db() as db:
            await db.execute(
                "UPDATE projects SET ralph_loop_current_issue = ?, ralph_loop_iteration = ? WHERE id = ?",
                (self.current_issue_id, self.iteration, self.project_id),
            )
            await db.commit()

    @staticmethod
    def _env(extra: dict[str, str]) -> dict[str, str]:
        """Return a copy of the current environment with extra vars merged in."""
        env = os.environ.copy()
        env.update(extra)
        return env
