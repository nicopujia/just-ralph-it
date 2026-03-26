"""Ralph autonomous coding loop — picks issues and solves them one at a time."""

import asyncio
import collections
import json
import logging
import os
import socket
from pathlib import Path
from typing import Optional

import httpx

from app import tasks
from app.config import RALPH_MODEL
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
        f"Solve this issue completely. Follow TDD: write tests "
        f"from acceptance criteria first, then implement.\n"
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
        self.dev_dir = project_dir + "-dev"
        self.project_name = project_name
        self.status: str = "stopped"
        self.current_issue_id: Optional[str] = None
        self.iteration: int = 0
        self.stdout_lines: collections.deque = collections.deque(
            maxlen=STDOUT_BUFFER_SIZE
        )
        self.user_github_name = user_github_name
        self.user_github_email = user_github_email
        self._subscribers: set[asyncio.Queue] = set()
        self._task: Optional[asyncio.Task] = None
        self.task_budget: int = task_budget
        self.tasks_completed: int = 0
        self._payment_event: asyncio.Event = asyncio.Event()
        # OpenCode server state
        self._opencode_port: int = 0
        self._opencode_process: Optional[asyncio.subprocess.Process] = None
        self._session_id: Optional[str] = None
        self._http_client: Optional[httpx.AsyncClient] = None

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
                    project_name,
                    issue_id,
                )
                # Reset dev worktree if it exists
                dev_dir = project_dir + "-dev"
                if Path(dev_dir).exists():
                    dev_reset = await asyncio.create_subprocess_exec(
                        "git",
                        "reset",
                        "--hard",
                        "HEAD",
                        cwd=dev_dir,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await dev_reset.communicate()
                # Reset main
                reset_proc = await asyncio.create_subprocess_exec(
                    "git",
                    "reset",
                    "--hard",
                    "HEAD",
                    cwd=project_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await reset_proc.communicate()
                # Clean up abandoned worktrees
                wt_proc = await asyncio.create_subprocess_exec(
                    "git",
                    "worktree",
                    "prune",
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
                        project_name,
                        "issue_update",
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
                i
                for i in all_issues
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
                        self.project_name,
                        "notification",
                        {
                            "id": notification_id,
                            "message": message,
                            "task_id": issue_id,
                            "created_at": created_at,
                        },
                    )

        except Exception:
            logger.exception(
                "Error polling for human blockers in project %s", self.project_name
            )

    # ------------------------------------------------------------------
    # OpenCode server lifecycle
    # ------------------------------------------------------------------

    async def _start_opencode_server(self) -> None:
        """Start the OpenCode HTTP server on a free port and wait for health."""
        # Find a free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            self._opencode_port = s.getsockname()[1]

        config = {
            "model": RALPH_MODEL,
            "permission": {"*": "allow"},
            "agent": {
                "ralph": {
                    "mode": "primary",
                    "description": "Autonomous coding agent that solves issues",
                    "prompt": RALPH_SYSTEM_PROMPT,
                    "tools": {
                        "bash": True,
                        "edit": True,
                        "write": True,
                        "read": True,
                        "grep": True,
                        "glob": True,
                        "webfetch": True,
                        "websearch": True,
                    },
                }
            },
            "default_agent": "ralph",
        }

        env = self._env({"OPENCODE_CONFIG_CONTENT": json.dumps(config)})

        self._opencode_process = await asyncio.create_subprocess_exec(
            "/home/linuxbrew/.linuxbrew/bin/opencode",
            "serve",
            "--port",
            str(self._opencode_port),
            "--hostname",
            "127.0.0.1",
            cwd=self.dev_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        base_url = f"http://127.0.0.1:{self._opencode_port}"
        self._http_client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(None),
        )

        # Poll for health
        deadline = asyncio.get_event_loop().time() + 60
        while asyncio.get_event_loop().time() < deadline:
            # Check if process died
            if self._opencode_process.returncode is not None:
                raise RuntimeError(
                    f"OpenCode server exited early with code "
                    f"{self._opencode_process.returncode}"
                )
            try:
                resp = await self._http_client.get(
                    "/global/health", timeout=httpx.Timeout(2.0)
                )
                if resp.status_code == 200:
                    logger.info(
                        "OpenCode server ready on port %d for project %s",
                        self._opencode_port,
                        self.project_name,
                    )
                    return
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                pass
            await asyncio.sleep(0.5)

        raise RuntimeError(
            f"OpenCode server did not become healthy within 60s "
            f"(port {self._opencode_port})"
        )

    async def _stop_opencode_server(self) -> None:
        """Shut down the OpenCode HTTP server and clean up resources."""
        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None

        if self._opencode_process and self._opencode_process.returncode is None:
            self._opencode_process.terminate()
            try:
                await asyncio.wait_for(self._opencode_process.wait(), timeout=10)
            except asyncio.TimeoutError:
                logger.warning("OpenCode server did not exit in 10s, killing it")
                self._opencode_process.kill()
                await self._opencode_process.wait()
            except Exception:
                pass

        self._opencode_process = None
        self._session_id = None

    # ------------------------------------------------------------------
    # OpenCode issue execution
    # ------------------------------------------------------------------

    async def _run_issue(self, prompt: str) -> int:
        """Run a single issue through OpenCode. Returns 0 on success, 1 on error."""
        assert self._http_client is not None

        try:
            # Create session
            resp = await self._http_client.post(
                "/session",
                json={"title": f"Issue {self.current_issue_id}"},
            )
            resp.raise_for_status()
            session_data = resp.json()
            self._session_id = session_data["id"]

            # Send prompt
            await self._http_client.post(
                f"/session/{self._session_id}/prompt_async",
                json={"parts": [{"type": "text", "text": prompt}]},
            )

            # Stream events until completion
            exit_code = await self._stream_events()
            return exit_code

        except Exception:
            logger.exception(
                "Project %s: error running issue %s via OpenCode",
                self.project_name,
                self.current_issue_id,
            )
            return 1
        finally:
            # Clean up session
            if self._session_id and self._http_client:
                try:
                    await self._http_client.delete(f"/session/{self._session_id}")
                except Exception:
                    pass
            self._session_id = None

    async def _stream_events(self) -> int:
        """Stream SSE events from OpenCode until session completes.

        Returns 0 on success (session.idle), 1 on error.
        """
        assert self._http_client is not None

        log_dir = Path(self.project_dir) / ".jri" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "ralph.log"
        log_file = open(log_path, "a", encoding="utf-8")

        try:
            async with self._http_client.stream("GET", "/event") as resp:
                buffer = ""
                async for chunk in resp.aiter_text():
                    # Check if opencode process died
                    if (
                        self._opencode_process
                        and self._opencode_process.returncode is not None
                    ):
                        logger.error(
                            "OpenCode server died during streaming (code %d)",
                            self._opencode_process.returncode,
                        )
                        return 1

                    buffer += chunk
                    # SSE messages are separated by double newlines
                    while "\n\n" in buffer:
                        message, buffer = buffer.split("\n\n", 1)
                        event = self._parse_sse(message)
                        if event is None:
                            continue

                        event_type = event.get("type", "")
                        props = event.get("properties", {})

                        # Filter by session ID
                        sid = props.get("sessionID", "")
                        if sid and sid != self._session_id:
                            continue

                        if event_type == "session.idle":
                            return 0

                        if event_type == "session.error":
                            error_data = props.get("error", {})
                            error_msg = error_data.get("data", {}).get(
                                "message"
                            ) or str(error_data)
                            logger.error(
                                "Project %s: session error: %s",
                                self.project_name,
                                error_msg,
                            )
                            return 1

                        # Extract display text
                        display_text = self._extract_display_text(event_type, props)
                        if not display_text:
                            continue

                        self.stdout_lines.append(display_text)

                        # Write to ralph log file
                        log_file.write(display_text + "\n")
                        log_file.flush()

                        # Publish to local subscribers
                        for q in self._subscribers.copy():
                            try:
                                q.put_nowait(display_text)
                            except asyncio.QueueFull:
                                pass

                        # Publish to SSE bus
                        await sse_bus.publish(
                            self.project_name,
                            "ralph_stdout",
                            {"line": display_text},
                        )

        except httpx.RemoteProtocolError:
            # Server closed the connection, check if it was intentional
            logger.warning("OpenCode SSE stream closed unexpectedly")
            return 1
        except Exception:
            logger.exception("Error streaming OpenCode events")
            return 1
        finally:
            log_file.close()

        # Stream ended without session.idle
        return 1

    @staticmethod
    def _parse_sse(message: str) -> dict | None:
        """Parse an SSE message into a dict, or None on failure."""
        data_lines = []
        for line in message.split("\n"):
            if line.startswith("data: "):
                data_lines.append(line[6:])
            elif line.startswith("data:"):
                data_lines.append(line[5:])
        if not data_lines:
            return None
        try:
            return json.loads("".join(data_lines))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _extract_display_text(event_type: str, props: dict) -> str | None:
        """Extract human-readable text from an OpenCode event."""
        if event_type != "message.part.updated":
            return None

        part = props.get("part", {})
        part_type = part.get("type", "")

        if part_type == "text":
            text = part.get("text", "").strip()
            return text if text else None

        if part_type == "tool_use":
            name = part.get("name", "")
            inp = part.get("input", {})
            if name == "bash":
                return f"$ {inp.get('command', '')}"
            if name == "write":
                return f"Writing {inp.get('file_path', '')}"
            if name == "edit":
                return f"Editing {inp.get('file_path', '')}"
            if name == "read":
                return f"Reading {inp.get('file_path', '')}"
            if name == "glob":
                return f"Searching {inp.get('pattern', '')}"
            if name == "grep":
                return f"Grepping {inp.get('pattern', '')}"
            return f"[{name}]"

        # Ignore other part types (reasoning, step-start, step-finish)
        return None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Core Ralph loop: pick issue, solve, push, repeat."""
        try:
            # --- Ensure dev worktree exists ---
            await self._ensure_dev_worktree()

            # --- Start OpenCode server ---
            await self._start_opencode_server()

            while self.status == "running":
                # --- Poll for human-assigned blockers ---
                await self._poll_for_human_blockers()

                # --- Get ready issues ---
                ready_issues = tasks.get_ready(self.project_dir)[:1]

                logger.info(
                    "Project %s: found %d ready issues",
                    self.project_name,
                    len(ready_issues),
                )

                if not ready_issues:
                    self.status = "stopped"
                    self._save_state()
                    await self._update_db_status("idle")

                    # --- Deploy if configured ---
                    await self._deploy_if_configured()

                    await sse_bus.publish(
                        self.project_name,
                        "ralph_status",
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
                            "Project %s: budget exhausted (%d/%d),"
                            " %d unpaid tasks remain",
                            self.project_name,
                            self.tasks_completed,
                            self.task_budget,
                            unpaid_count,
                        )
                        self.status = "payment_required"
                        self._save_state()
                        await self._update_db_status("payment_required")
                        await sse_bus.publish(
                            self.project_name,
                            "payment_required",
                            {
                                "unpaid_count": unpaid_count,
                                "project_name": self.project_name,
                            },
                        )
                        await sse_bus.publish(
                            self.project_name,
                            "ralph_status",
                            {
                                "status": "payment_required",
                                "unpaid_count": unpaid_count,
                            },
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
                            self.project_name,
                            self.task_budget,
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
                        self.project_dir,
                        self.current_issue_id,
                        assignee="ralph",
                    )
                    await sse_bus.publish(
                        self.project_name,
                        "issue_update",
                        {"issue_id": self.current_issue_id, "action": "claimed"},
                    )

                    # --- Commit claim on main and sync to dev ---
                    await self._git_exec(self.project_dir, "add", "-A")
                    await self._git_exec(
                        self.project_dir,
                        "commit",
                        "-m",
                        f"claim {self.current_issue_id}",
                    )
                    await self._sync_dev_with_main()

                    # --- Clear stdout for new issue ---
                    self.stdout_lines.clear()
                    await sse_bus.publish(self.project_name, "ralph_stdout_clear", {})

                    # --- Build prompt ---
                    prompt = build_ralph_prompt(
                        issue,
                        self.user_github_name,
                        self.user_github_email,
                    )

                    # --- Run OpenCode ---
                    logger.info(
                        "Project %s: starting OpenCode for issue %s (prompt: %d chars)",
                        self.project_name,
                        self.current_issue_id,
                        len(prompt),
                    )
                    exit_code = await self._run_issue(prompt)
                    logger.info(
                        "Project %s: OpenCode finished with code %d",
                        self.project_name,
                        exit_code,
                    )

                    if exit_code != 0:
                        await self._recover(self.current_issue_id)
                        continue

                    # --- Merge dev to main and push ---
                    await self._merge_dev_to_main()

                    # --- Check if issue was closed ---
                    issue_data = tasks.get_task(
                        self.dev_dir,
                        self.current_issue_id,
                    )
                    if issue_data and issue_data.get("status") != "done":
                        logger.warning(
                            "Issue %s was not closed by Ralph after iteration %d",
                            self.current_issue_id,
                            self.iteration,
                        )

                    # Notify frontend of issue state change
                    await sse_bus.publish(
                        self.project_name,
                        "issue_update",
                        {"issue_id": self.current_issue_id, "action": "completed"},
                    )
                    self.tasks_completed += 1

                except Exception:
                    logger.exception(
                        "Iteration %d crashed on issue %s in project %s",
                        self.iteration,
                        self.current_issue_id,
                        self.project_name,
                    )
                    await self._recover(self.current_issue_id)
                    continue

        except Exception:
            logger.exception("Ralph loop crashed for project %s", self.project_name)
        finally:
            await self._stop_opencode_server()
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
            deploy_subdomain = (
                row_dict.get("deploy_subdomain")
                or f"{self.project_name.lower()}.{github_username}"
            )
            # Ensure subdomain uses the new {project}.{username} format
            if "." not in deploy_subdomain:
                deploy_subdomain = f"{deploy_subdomain}.{github_username}"

            from app.deploy_manager import deploy_dynamic

            await deploy_dynamic(
                self.project_name,
                self.project_dir,
                deploy_start_command or "",
                deploy_port or 9000,
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
                self.project_name,
                "ralph_status",
                {
                    "status": "deployed",
                    "url": f"https://{deploy_subdomain}.justralph.it",
                },
            )
            logger.info(
                "Deployed project %s to https://%s.justralph.it",
                self.project_name,
                deploy_subdomain,
            )

        except Exception:
            logger.exception("Deployment failed for project %s", self.project_name)

    async def _recover(self, issue_id: str) -> None:
        """Reset git state, reopen issue, log crash, and publish event."""
        logger.warning(
            "Recovering from crash on issue %s in project %s",
            issue_id,
            self.project_name,
        )

        recovery_msg = f"Crashed on issue {issue_id}, recovering..."
        await sse_bus.publish(
            self.project_name,
            "ralph_stdout",
            {"line": recovery_msg},
        )

        # Reset dev worktree (where Ralph was working)
        await self._git_exec(self.dev_dir, "reset", "--hard", "HEAD")

        # Clean up abandoned worktrees
        await self._git_exec(self.project_dir, "worktree", "prune")

        # Reopen issue on main and commit
        try:
            tasks.set_status(self.project_dir, issue_id, "todo")
            await self._git_exec(self.project_dir, "add", "-A")
            await self._git_exec(
                self.project_dir,
                "commit",
                "-m",
                f"reopen {issue_id}",
            )
        except Exception:
            logger.warning("Could not reopen issue %s during recovery", issue_id)

        await sse_bus.publish(
            self.project_name,
            "ralph_status",
            {"status": "crash_recovery", "issue_id": issue_id},
        )
        await sse_bus.publish(
            self.project_name,
            "issue_update",
            {"issue_id": issue_id, "action": "reopened"},
        )

        logger.info(
            "Recovery complete for issue %s in project %s",
            issue_id,
            self.project_name,
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
        # Abort running session if any
        if self._session_id and self._http_client:
            try:
                await self._http_client.post(f"/session/{self._session_id}/abort")
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
    # Worktree helpers
    # ------------------------------------------------------------------

    async def _git_exec(self, cwd: str, *args: str) -> tuple[int, str, str]:
        """Run a git command and return (returncode, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        assert proc.returncode is not None
        return (
            proc.returncode,
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
        )

    async def _ensure_dev_worktree(self) -> None:
        """Create the dev worktree and branch if they don't exist."""
        import shutil

        dev_path = Path(self.dev_dir)
        if dev_path.exists():
            if (dev_path / ".git").exists():
                return
            # Corrupt worktree -- remove and recreate
            shutil.rmtree(self.dev_dir, ignore_errors=True)
            await self._git_exec(self.project_dir, "worktree", "prune")

        # Try creating with new branch
        rc, _, stderr = await self._git_exec(
            self.project_dir,
            "worktree",
            "add",
            self.dev_dir,
            "-b",
            "dev",
        )
        if rc != 0:
            if "already exists" in stderr:
                rc2, _, stderr2 = await self._git_exec(
                    self.project_dir,
                    "worktree",
                    "add",
                    self.dev_dir,
                    "dev",
                )
                if rc2 != 0:
                    raise RuntimeError(f"Failed to create dev worktree: {stderr2}")
            else:
                raise RuntimeError(f"Failed to create dev worktree: {stderr}")
        logger.info("Dev worktree ready at %s", self.dev_dir)

    async def _sync_dev_with_main(self) -> None:
        """Pull main and merge into dev so dev has the latest."""
        await self._git_exec(self.project_dir, "pull", "--ff-only")
        rc, _, stderr = await self._git_exec(self.dev_dir, "merge", "main", "--no-edit")
        if rc != 0:
            logger.warning("Merge main into dev failed, resetting dev: %s", stderr)
            await self._git_exec(self.dev_dir, "reset", "--hard", "main")

    async def _merge_dev_to_main(self) -> None:
        """Merge dev into main and push. Pre-merge sync resolves conflicts in dev."""
        # Pre-merge sync: merge main into dev (catches late main changes)
        rc, _, stderr = await self._git_exec(self.dev_dir, "merge", "main", "--no-edit")
        if rc != 0:
            logger.warning("Pre-merge sync failed, aborting: %s", stderr)
            await self._git_exec(self.dev_dir, "merge", "--abort")
            raise RuntimeError(f"Pre-merge sync failed: {stderr}")

        # Merge dev into main (should be clean/fast-forward)
        rc, _, stderr = await self._git_exec(
            self.project_dir, "merge", "dev", "--no-edit"
        )
        if rc != 0:
            logger.warning("Merge dev->main failed, aborting: %s", stderr)
            await self._git_exec(self.project_dir, "merge", "--abort")
            raise RuntimeError(f"Merge dev into main failed: {stderr}")

        # Push main
        rc, _, stderr = await self._git_exec(self.project_dir, "push")
        if rc != 0:
            logger.warning("Push main failed: %s", stderr)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
                "UPDATE projects SET ralph_loop_current_issue = ?,"
                " ralph_loop_iteration = ? WHERE id = ?",
                (self.current_issue_id, self.iteration, self.project_id),
            )
            await db.commit()

    @staticmethod
    def _env(extra: dict[str, str]) -> dict[str, str]:
        """Return a copy of the current environment with extra vars merged in."""
        env = os.environ.copy()
        env.update(extra)
        return env
