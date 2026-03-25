"""Ralph loop control endpoints."""

import asyncio
import json
import logging
import stripe
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app import tasks
from app.auth_utils import get_current_user
from app.config import BASE_URL, DATA_DIR, STRIPE_SECRET_KEY
from app.database import get_db
from app.ralph_loop import RalphLoop
from app.routers.projects import _get_issue_count, _get_project_dir

logger = logging.getLogger(__name__)

stripe.api_key = STRIPE_SECRET_KEY
if STRIPE_SECRET_KEY.startswith("pk_"):
    logger.warning(
        "STRIPE_SECRET_KEY starts with 'pk_' — this looks like a publishable key, not a secret key"
    )

router = APIRouter(prefix="/api/projects", tags=["ralph"])

# Global dict of active loops keyed by project name
_loops: dict[str, RalphLoop] = {}


async def _get_project(name: str, user: dict) -> dict:
    """Look up project by name for the authenticated user. Returns row dict."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, name, ralph_loop_status, ralph_loop_current_issue, ralph_loop_iteration, stripe_payment_id "
            "FROM projects WHERE user_id = ? AND name = ?",
            (user["id"], name),
        )
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return dict(row)


async def _start_ralph_loop(name: str, project: dict, user: dict) -> None:
    """Shared helper to start the Ralph loop for a project."""
    if name in _loops and _loops[name].status == "running":
        return

    github_username: str = user["github_username"]
    project_dir = str(DATA_DIR / github_username / name)

    user_name = user.get("github_name") or github_username
    user_email = user.get("github_email") or f"{github_username}@users.noreply.github.com"

    loop = RalphLoop(
        project_id=project["id"],
        project_dir=project_dir,
        project_name=name,
        user_github_name=user_name,
        user_github_email=user_email,
    )
    _loops[name] = loop
    await loop.start()


@router.post("/{name}/ralph/checkout")
async def ralph_checkout(name: str, user: dict = Depends(get_current_user)):
    """Create a Stripe checkout session or start Ralph if already paid."""
    project = await _get_project(name, user)
    project_dir = await _get_project_dir(name, user)

    # Already paid — just start Ralph
    if project.get("stripe_payment_id"):
        await _start_ralph_loop(name, project, user)
        return {"free": True, "redirect": None}

    issue_count = _get_issue_count(project_dir)
    if issue_count == 0:
        raise HTTPException(status_code=400, detail="No issues found — nothing to bid on")

    unit_amount = issue_count * 100  # $1 per issue in cents

    # Create Stripe Checkout Session
    checkout_session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": unit_amount,
                    "product_data": {
                        "name": f"Just Ralph It — {name} ({issue_count} issues)",
                    },
                },
                "quantity": 1,
            }
        ],
        success_url=f"{BASE_URL}/projects/{name}?tab=ralph&payment=success&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE_URL}/projects/{name}?payment=cancel",
        client_reference_id=str(project["id"]),
        metadata={"user_id": str(user["id"]), "project_name": name},
    )

    return {"free": False, "redirect": checkout_session.url}


@router.get("/{name}/ralph/payment-callback")
async def ralph_payment_callback(
    name: str,
    session_id: str = Query(...),
    user: dict = Depends(get_current_user),
):
    """Verify Stripe payment and start Ralph loop."""
    project = await _get_project(name, user)

    # Retrieve the Stripe session
    session = stripe.checkout.Session.retrieve(session_id)

    if session.payment_status != "paid":
        raise HTTPException(status_code=402, detail="Payment not confirmed")

    if session.client_reference_id != str(project["id"]):
        raise HTTPException(status_code=400, detail="Session does not match project")

    # Store payment ID
    async with get_db() as db:
        await db.execute(
            "UPDATE projects SET stripe_payment_id = ? WHERE id = ?",
            (session_id, project["id"]),
        )
        await db.commit()

    # Start Ralph loop
    await _start_ralph_loop(name, project, user)

    return {"status": "started"}


@router.post("/{name}/ralph/start")
async def ralph_start(name: str, user: dict = Depends(get_current_user)):
    """Begin the Ralph loop for a project."""
    project = await _get_project(name, user)

    if name in _loops and _loops[name].status == "running":
        raise HTTPException(status_code=409, detail="Ralph loop is already running")

    await _start_ralph_loop(name, project, user)

    return {"status": "running"}


@router.post("/{name}/ralph/stop")
async def ralph_stop(name: str, user: dict = Depends(get_current_user)):
    """Gracefully stop the Ralph loop after the current iteration."""
    project = await _get_project(name, user)

    loop = _loops.get(name)
    if loop is not None:
        current_issue = loop.current_issue_id
        project_dir = loop.project_dir
        await loop.stop()

        # Clean up git state and move task back to todo
        try:
            reset_proc = await asyncio.create_subprocess_exec(
                "git", "reset", "--hard", "HEAD",
                cwd=project_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await reset_proc.communicate()

            wt_proc = await asyncio.create_subprocess_exec(
                "git", "worktree", "prune",
                cwd=project_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await wt_proc.communicate()

            if current_issue:
                try:
                    task_data = tasks.get_task(project_dir, current_issue)
                    if task_data and task_data.get("status") == "doing":
                        tasks.set_status(project_dir, current_issue, "todo")
                except Exception:
                    logger.warning("Could not reopen issue %s on stop", current_issue)
        except Exception:
            logger.exception("Error cleaning up git state on stop for %s", name)

        return {"status": "stopped"}

    # No in-memory loop — DB may be stale (e.g., after service restart).
    # Reset DB status to idle.
    async with get_db() as db:
        await db.execute(
            "UPDATE projects SET ralph_loop_status = 'idle' WHERE id = ?",
            (project["id"],),
        )
        await db.commit()
    return {"status": "stopped"}


@router.post("/{name}/ralph/resume")
async def ralph_resume(name: str, user: dict = Depends(get_current_user)):
    """Resume (same as start) the Ralph loop."""
    return await ralph_start(name, user=user)


@router.get("/{name}/ralph/stream")
async def ralph_stream(name: str, user: dict = Depends(get_current_user)):
    """SSE endpoint that streams Ralph's stdout in real time."""
    await _get_project(name, user)
    loop = _loops.get(name)
    if loop is None:
        raise HTTPException(status_code=404, detail="No active Ralph loop")

    async def _generate():
        queue = loop.subscribe()
        try:
            while True:
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps({'line': line})}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                # Stop streaming once loop is no longer running
                if loop.status not in ("running", "stopping"):
                    break
        except asyncio.CancelledError:
            pass
        finally:
            loop.unsubscribe(queue)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{name}/notifications")
async def get_notifications(name: str, user: dict = Depends(get_current_user)):
    """Return unacknowledged notifications for a project."""
    project = await _get_project(name, user)
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, message, task_id, created_at "
            "FROM notifications "
            "WHERE project_id = ? AND acknowledged = 0 "
            "ORDER BY created_at DESC",
            (project["id"],),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@router.post("/{name}/notifications/{notification_id}/acknowledge")
async def acknowledge_notification(
    name: str, notification_id: int, user: dict = Depends(get_current_user)
):
    """Mark a notification as acknowledged."""
    project = await _get_project(name, user)
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE notifications SET acknowledged = 1 "
            "WHERE id = ? AND project_id = ?",
            (notification_id, project["id"]),
        )
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Notification not found")
    return {"status": "acknowledged"}


@router.get("/{name}/ralph/status")
async def ralph_status(name: str, user: dict = Depends(get_current_user)):
    """Return current Ralph loop state."""
    project = await _get_project(name, user)

    loop = _loops.get(name)
    if loop is None:
        # No in-memory loop — DB may say "running" if service restarted.
        # Correct stale status.
        db_status = project.get("ralph_loop_status", "idle")
        if db_status == "running":
            db_status = "idle"
            async with get_db() as db:
                await db.execute(
                    "UPDATE projects SET ralph_loop_status = 'idle' WHERE id = ?",
                    (project["id"],),
                )
                await db.commit()

        return {
            "status": db_status,
            "current_issue": project.get("ralph_loop_current_issue"),
            "iteration": project.get("ralph_loop_iteration", 0),
            "recent_output": [],
        }

    recent = list(loop.stdout_lines)[-50:]
    return {
        "status": loop.status,
        "current_issue": loop.current_issue_id,
        "iteration": loop.iteration,
        "recent_output": recent,
    }
