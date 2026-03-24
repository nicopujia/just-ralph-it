"""Ralph autonomous coding loop — picks issues and solves them one at a time."""

import asyncio
import collections
import json
import logging
from pathlib import Path
from typing import Optional

from app import issues
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
        f"Read CLAUDE.md in the project root and any relevant subdirectories.\n"
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
        f"Then: mv .ralph/issues/open/{issue_id}.yaml .ralph/issues/closed/"
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

    @staticmethod
    async def check_interrupted(project_dir: str, project_name: str) -> None:
        """Check if a previous loop was interrupted and clean up."""
        state_path = Path(project_dir) / ".ralph_state"
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
                # Reopen the issue
                if issue_id:
                    try:
                        issues.set_status(project_dir, issue_id, "open")
                    except Exception:
                        logger.warning("Could not reopen issue %s", issue_id)
                    await sse_bus.publish(
                        project_name, "issue_update",
                        {"issue_id": issue_id, "action": "reopened"},
                    )
                # Clean up state file
                state_path.unlink(missing_ok=True)
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
            all_issues = issues.list_all(self.project_dir)

            human_issues = [
                i for i in all_issues
                if i.get("assignee") == "Human" and i.get("status") == "open"
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
                        "WHERE project_id = ? AND beads_issue_id = ?",
                        (self.project_id, issue_id),
                    )
                    existing = await cursor.fetchone()
                    if existing:
                        continue

                    message = f"Ralph needs help: {title}"
                    cursor = await db.execute(
                        "INSERT INTO notifications (project_id, message, beads_issue_id) "
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
                            "beads_issue_id": issue_id,
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
                ready_issues = issues.get_ready(self.project_dir)[:1]

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

                issue = ready_issues[0]
                self.current_issue_id = issue.get("id", "")
                self.iteration += 1

                # --- Save state ---
                self._save_state()
                await self._update_db_issue()

                try:
                    # --- Claim ---
                    issues.update_field(
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
                        "--max-budget-usd", "10",
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
                    issue_data = issues.get_issue(
                        self.project_dir, self.current_issue_id,
                    )
                    if issue_data and issue_data.get("status") != "closed":
                        logger.warning(
                            "Issue %s was not closed by Ralph after iteration %d",
                            self.current_issue_id, self.iteration,
                        )

                    # Notify frontend of issue state change
                    await sse_bus.publish(self.project_name, "issue_update", {"issue_id": self.current_issue_id, "action": "completed"})

                except Exception:
                    logger.exception(
                        "Iteration %d crashed on issue %s in project %s",
                        self.iteration, self.current_issue_id, self.project_name,
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
                    "SELECT deploy_type, deploy_port, deploy_start_command, deploy_subdomain "
                    "FROM projects WHERE id = ?",
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
            deploy_subdomain = row_dict.get("deploy_subdomain") or self.project_name.lower()

            if deploy_type == "dynamic":
                from app.deploy_manager import deploy_dynamic
                await deploy_dynamic(
                    self.project_name, self.project_dir,
                    deploy_start_command or "", deploy_port or 9000,
                )
            elif deploy_type == "static":
                from app.deploy_manager import deploy_static
                await deploy_static(self.project_name, self.project_dir)

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

        # Reopen issue
        try:
            issues.set_status(self.project_dir, issue_id, "open")
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

    async def stop(self) -> None:
        """Gracefully stop after the current iteration finishes."""
        if self.status != "running":
            return
        self.status = "stopping"
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

        self._save_stdout()

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

    def _save_stdout(self) -> None:
        """Persist stdout to disk for recovery."""
        stdout_path = Path(self.project_dir) / ".ralph_stdout"
        stdout_path.write_text("\n".join(self.stdout_lines))

    def _save_state(self) -> None:
        """Persist loop state to .ralph_state in the project directory."""
        state = {
            "project_id": self.project_id,
            "status": self.status,
            "current_issue_id": self.current_issue_id,
            "iteration": self.iteration,
        }
        state_path = Path(self.project_dir) / ".ralph_state"
        state_path.write_text(json.dumps(state, indent=2))

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
        import os
        env = os.environ.copy()
        env.update(extra)
        return env
