import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth_utils import decode_session_token
from app.config import DATA_DIR
from app.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["pages"])

_templates_dir = Path(__file__).resolve().parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


async def _is_logged_in(request: Request) -> bool:
    """Check if the user has a valid session, without raising errors."""
    token = request.cookies.get("session")
    if not token:
        return False
    try:
        user_id = decode_session_token(token)
    except Exception:
        return False
    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
    return row is not None


@router.get("/")
async def landing(request: Request):
    logged_in = await _is_logged_in(request)
    return templates.TemplateResponse("landing.html", {"request": request, "logged_in": logged_in})


@router.get("/projects")
async def dashboard(request: Request):
    token = request.cookies.get("session")
    if not token:
        return RedirectResponse(url="/", status_code=302)
    try:
        user_id = decode_session_token(token)
    except Exception:
        return RedirectResponse(url="/", status_code=302)
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
    if row is None:
        return RedirectResponse(url="/", status_code=302)
    user = dict(row)
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user})


# Legacy redirects
@router.get("/dashboard")
async def dashboard_redirect():
    return RedirectResponse(url="/projects", status_code=301)


@router.get("/project/{name}")
async def project_redirect(name: str):
    return RedirectResponse(url=f"/projects/{name}", status_code=301)


@router.get("/new")
async def new_project(request: Request):
    if not await _is_logged_in(request):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("new_project.html", {"request": request})


@router.get("/projects/{name}")
async def project_page(request: Request, name: str):
    token = request.cookies.get("session")
    if not token:
        return RedirectResponse(url="/", status_code=302)
    try:
        user_id = decode_session_token(token)
    except Exception:
        return RedirectResponse(url="/", status_code=302)

    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        user_row = await cursor.fetchone()
    if user_row is None:
        return RedirectResponse(url="/", status_code=302)
    user = dict(user_row)

    # Verify project belongs to user
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, name, description, github_repo_url, ralph_loop_status, "
            "ralph_loop_current_issue, ralph_loop_iteration, ralph_session_id, stripe_payment_id "
            "FROM projects WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        project_row = await cursor.fetchone()
    if project_row is None:
        from fastapi.responses import HTMLResponse

        return HTMLResponse(status_code=404, content="Project not found")
    project = dict(project_row)

    # Check for ?payment=success&session_id=...
    payment = request.query_params.get("payment")
    session_id = request.query_params.get("session_id")
    if payment == "success" and session_id:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"http://127.0.0.1:8000/api/projects/{name}/ralph/payment-callback",
                    params={"session_id": session_id},
                    cookies={"session": token},
                    timeout=30,
                )
                if resp.status_code == 200:
                    logger.info("Payment callback succeeded for project %s", name)
                else:
                    logger.warning(
                        "Payment callback returned %s for project %s",
                        resp.status_code,
                        name,
                    )
        except Exception:
            logger.exception("Payment callback failed for project %s", name)

    valid_tabs = {"overview", "tasks", "ralph", "uploads", "env"}
    active_tab = request.query_params.get("tab", "overview")
    if active_tab not in valid_tabs:
        active_tab = "overview"

    return templates.TemplateResponse(
        "project.html", {"request": request, "user": user, "project": project, "active_tab": active_tab}
    )
