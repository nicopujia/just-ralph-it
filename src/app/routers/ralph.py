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
from app.routers.projects import _get_project_dir
from app.freelist import is_free_user
from app.whitelist import check_whitelist

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
            "SELECT id, name, ralph_loop_status, ralph_loop_current_issue, "
            "ralph_loop_iteration, stripe_payment_id, paid_task_count, "
            "base_fee_paid "
            "FROM projects WHERE user_id = ? AND name = ?",
            (user["id"], name),
        )
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return dict(row)


def _count_non_done_tasks(project_dir: str) -> int:
    """Count tasks that are not done (draft + todo + doing)."""
    all_tasks = tasks.list_all(project_dir)
    return sum(1 for t in all_tasks if t.get("status") != "done")


def _calc_budget(project_dir: str, paid_task_count: int, free: bool) -> int:
    """Calculate the task budget for the Ralph loop.

    For free users returns a virtually unlimited budget.
    For paid users returns paid_task_count minus already doing/done tasks.
    """
    if free:
        return 999999
    all_tasks = tasks.list_all(project_dir)
    done_doing = sum(1 for t in all_tasks if t.get("status") in ("done", "doing"))
    return max(paid_task_count - done_doing, 0)


async def _start_ralph_loop(name: str, project: dict, user: dict, budget: int = 999999) -> None:
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
        task_budget=budget,
    )
    _loops[name] = loop
    await loop.start()


@router.post("/{name}/ralph/checkout")
async def ralph_checkout(name: str, user: dict = Depends(get_current_user)):
    """Create a Stripe checkout session for unpaid tasks, or start Ralph directly."""
    check_whitelist(user)
    project = await _get_project(name, user)
    project_dir = await _get_project_dir(name, user)

    free = is_free_user(user)
    total_tasks = _count_non_done_tasks(project_dir)
    paid_task_count = project.get("paid_task_count", 0)
    base_fee_paid = bool(project.get("base_fee_paid", 0))
    unpaid = total_tasks - paid_task_count

    # Nothing new to pay: start directly
    if not free and unpaid <= 0 and base_fee_paid:
        budget = _calc_budget(project_dir, paid_task_count, free=False)
        await _start_ralph_loop(name, project, user, budget=budget)
        return {"free": False, "redirect": None}

    if total_tasks == 0 and base_fee_paid:
        raise HTTPException(status_code=400, detail="No tasks found")

    # Pricing: $10 base (one-time) + $5 per unpaid task
    base_amount = 0 if base_fee_paid else 1000  # $10 in cents
    task_amount = max(unpaid, 0) * 500  # $5 per task in cents
    unit_amount = base_amount + task_amount

    if unit_amount <= 0:
        # Everything already paid, just start
        budget = _calc_budget(project_dir, paid_task_count, free=False)
        await _start_ralph_loop(name, project, user, budget=budget)
        return {"free": False, "redirect": None}

    # Build description
    parts = []
    if not base_fee_paid:
        parts.append("Project base ($10)")
    if unpaid > 0:
        parts.append(f"{unpaid} tasks \u00d7 $5")
    description = " + ".join(parts)

    # Apply 100% coupon for free users
    discounts = []
    if free:
        coupon_id = "jri-free-100"
        try:
            stripe.Coupon.retrieve(coupon_id)
        except stripe.InvalidRequestError:
            stripe.Coupon.create(
                id=coupon_id,
                percent_off=100,
                duration="forever",
                name="JRI Free User",
            )
        discounts = [{"coupon": coupon_id}]

    # Create Stripe Checkout Session
    checkout_session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": unit_amount,
                    "product_data": {
                        "name": f"Just Ralph It \u2014 {name}",
                        "description": description,
                    },
                },
                "quantity": 1,
            }
        ],
        discounts=discounts if discounts else [],
        success_url=f"{BASE_URL}/projects/{name}?tab=ralph&payment=success&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE_URL}/projects/{name}?payment=cancel",
        client_reference_id=str(project["id"]),
        metadata={
            "user_id": str(user["id"]),
            "project_name": name,
            "unpaid_count": str(max(unpaid, 0)),
            "includes_base_fee": "1" if not base_fee_paid else "0",
        },
    )

    return {"free": False, "redirect": checkout_session.url}


@router.get("/{name}/ralph/payment-callback")
async def ralph_payment_callback(
    name: str,
    session_id: str = Query(...),
    user: dict = Depends(get_current_user),
):
    """Verify Stripe payment, update paid_task_count, and start Ralph loop."""
    project = await _get_project(name, user)

    # Retrieve the Stripe session
    session = stripe.checkout.Session.retrieve(session_id)

    if session.payment_status != "paid":
        raise HTTPException(status_code=402, detail="Payment not confirmed")

    if session.client_reference_id != str(project["id"]):
        raise HTTPException(status_code=400, detail="Session does not match project")

    # Get the unpaid count that was stored in checkout metadata
    unpaid_count = int(session.metadata.get("unpaid_count", "0"))
    includes_base_fee = session.metadata.get("includes_base_fee", "0") == "1"

    # Update payment ID, increment paid_task_count, and mark base fee paid
    async with get_db() as db:
        if includes_base_fee:
            await db.execute(
                "UPDATE projects SET stripe_payment_id = ?, "
                "paid_task_count = paid_task_count + ?, "
                "base_fee_paid = 1 WHERE id = ?",
                (session_id, unpaid_count, project["id"]),
            )
        else:
            await db.execute(
                "UPDATE projects SET stripe_payment_id = ?, "
                "paid_task_count = paid_task_count + ? WHERE id = ?",
                (session_id, unpaid_count, project["id"]),
            )
        await db.commit()

    # Re-read updated paid_task_count
    project = await _get_project(name, user)
    project_dir = await _get_project_dir(name, user)
    budget = _calc_budget(project_dir, project["paid_task_count"], free=False)

    # Resume existing loop if paused, otherwise start a new one
    loop = _loops.get(name)
    if loop is not None and loop.status == "payment_required":
        await loop.resume_after_payment(budget)
    else:
        await _start_ralph_loop(name, project, user, budget=budget)

    return {"status": "started", "paid_task_count": project["paid_task_count"]}


@router.post("/{name}/ralph/start")
async def ralph_start(name: str, user: dict = Depends(get_current_user)):
    """Begin the Ralph loop for a project."""
    check_whitelist(user)
    project = await _get_project(name, user)

    if name in _loops and _loops[name].status == "running":
        raise HTTPException(status_code=409, detail="Ralph loop is already running")

    free = is_free_user(user)
    project_dir = await _get_project_dir(name, user)
    budget = _calc_budget(project_dir, project.get("paid_task_count", 0), free)

    await _start_ralph_loop(name, project, user, budget=budget)

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
    """Resume the Ralph loop with recalculated budget."""
    check_whitelist(user)
    project = await _get_project(name, user)

    free = is_free_user(user)
    project_dir = await _get_project_dir(name, user)
    budget = _calc_budget(project_dir, project.get("paid_task_count", 0), free)

    # If the loop is paused waiting for payment, resume it in-place
    loop = _loops.get(name)
    if loop is not None and loop.status == "payment_required":
        await loop.resume_after_payment(budget)
        return {"status": "running"}

    if loop is not None and loop.status == "running":
        raise HTTPException(status_code=409, detail="Ralph loop is already running")

    await _start_ralph_loop(name, project, user, budget=budget)

    return {"status": "running"}


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
                if loop.status not in ("running", "stopping", "payment_required"):
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


@router.get("/{name}/ralph/payment-status")
async def ralph_payment_status(name: str, user: dict = Depends(get_current_user)):
    """Return payment delta info for the frontend."""
    project = await _get_project(name, user)
    project_dir = await _get_project_dir(name, user)

    free = is_free_user(user)
    paid_task_count = project.get("paid_task_count", 0)
    base_paid = bool(project.get("base_fee_paid", 0))
    total_tasks = _count_non_done_tasks(project_dir)
    unpaid = max(total_tasks - paid_task_count, 0)

    return {
        "paid_task_count": paid_task_count,
        "total_tasks": total_tasks,
        "unpaid": unpaid,
        "base_paid": base_paid,
        "free_user": free,
    }


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
