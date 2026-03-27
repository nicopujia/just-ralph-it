import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth_utils import decode_session_token
from app.database import get_db
from app.docs import DOCS_PAGES, render_doc_page

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
        session_data = decode_session_token(token)
        user_id = (
            session_data["uid"] if isinstance(session_data, dict) else session_data
        )
    except Exception:
        return False
    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
    return row is not None


@router.get("/")
async def landing(request: Request):
    logged_in = await _is_logged_in(request)
    return templates.TemplateResponse(
        "landing.html", {"request": request, "logged_in": logged_in}
    )


@router.get("/projects")
async def dashboard(request: Request):
    token = request.cookies.get("session")
    if not token:
        return RedirectResponse(url="/", status_code=302)
    try:
        session_data = decode_session_token(token)
        user_id = (
            session_data["uid"] if isinstance(session_data, dict) else session_data
        )
    except Exception:
        return RedirectResponse(url="/", status_code=302)
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
    if row is None:
        return RedirectResponse(url="/", status_code=302)
    user = dict(row)
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "user": user}
    )


# Legacy redirects
@router.get("/dashboard")
async def dashboard_redirect():
    return RedirectResponse(url="/projects", status_code=301)


@router.get("/project/{name}")
async def project_redirect(name: str):
    return RedirectResponse(url=f"/projects/{name}", status_code=301)


@router.get("/new")
async def new_project(request: Request):
    token = request.cookies.get("session")
    if not token:
        return RedirectResponse(url="/", status_code=302)
    try:
        session_data = decode_session_token(token)
        user_id = (
            session_data["uid"] if isinstance(session_data, dict) else session_data
        )
    except Exception:
        return RedirectResponse(url="/", status_code=302)
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
    if row is None:
        return RedirectResponse(url="/", status_code=302)
    user = dict(row)
    return templates.TemplateResponse(
        "new_project.html", {"request": request, "user": user}
    )


@router.get("/projects/{name}")
async def project_page(request: Request, name: str):
    token = request.cookies.get("session")
    if not token:
        return RedirectResponse(url="/", status_code=302)
    try:
        session_data = decode_session_token(token)
        user_id = (
            session_data["uid"] if isinstance(session_data, dict) else session_data
        )
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
            "ralph_loop_current_issue, ralph_loop_iteration,"
            " ralph_session_id, stripe_payment_id "
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

    response = templates.TemplateResponse(
        "project.html",
        {
            "request": request,
            "user": user,
            "project": project,
            "active_tab": active_tab,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


# Public docs routes
@router.get("/docs")
async def docs_index(request: Request):
    logged_in = await _is_logged_in(request)
    return templates.TemplateResponse(
        "docs_index.html", {"request": request, "logged_in": logged_in}
    )


@router.get("/docs/overview")
async def docs_overview(request: Request):
    logged_in = await _is_logged_in(request)
    page = DOCS_PAGES["overview"]
    content = render_doc_page(page["body"])
    return templates.TemplateResponse(
        "docs_page.html",
        {
            "request": request,
            "logged_in": logged_in,
            "page_title": page["title"],
            "page_slug": "overview",
            "content": content,
        },
    )


@router.get("/docs/agents")
async def docs_agents(request: Request):
    logged_in = await _is_logged_in(request)
    page = DOCS_PAGES["agents"]
    content = render_doc_page(page["body"])
    return templates.TemplateResponse(
        "docs_page.html",
        {
            "request": request,
            "logged_in": logged_in,
            "page_title": page["title"],
            "page_slug": "agents",
            "content": content,
        },
    )


@router.get("/docs/best-practices")
async def docs_best_practices(request: Request):
    logged_in = await _is_logged_in(request)
    page = DOCS_PAGES["best-practices"]
    content = render_doc_page(page["body"])
    return templates.TemplateResponse(
        "docs_page.html",
        {
            "request": request,
            "logged_in": logged_in,
            "page_title": page["title"],
            "page_slug": "best-practices",
            "content": content,
        },
    )


@router.get("/docs/pricing")
async def docs_pricing(request: Request):
    logged_in = await _is_logged_in(request)
    page = DOCS_PAGES["pricing"]
    content = render_doc_page(page["body"])
    return templates.TemplateResponse(
        "docs_page.html",
        {
            "request": request,
            "logged_in": logged_in,
            "page_title": page["title"],
            "page_slug": "pricing",
            "content": content,
        },
    )


@router.get("/docs/privacy-security")
async def docs_privacy_security(request: Request):
    logged_in = await _is_logged_in(request)
    page = DOCS_PAGES["privacy-security"]
    content = render_doc_page(page["body"])
    return templates.TemplateResponse(
        "docs_page.html",
        {
            "request": request,
            "logged_in": logged_in,
            "page_title": page["title"],
            "page_slug": "privacy-security",
            "content": content,
        },
    )


@router.get("/docs/deployment")
async def docs_deployment(request: Request):
    logged_in = await _is_logged_in(request)
    page = DOCS_PAGES["deployment"]
    content = render_doc_page(page["body"])
    return templates.TemplateResponse(
        "docs_page.html",
        {
            "request": request,
            "logged_in": logged_in,
            "page_title": page["title"],
            "page_slug": "deployment",
            "content": content,
        },
    )


@router.get("/docs/faq")
async def docs_faq(request: Request):
    logged_in = await _is_logged_in(request)
    page = DOCS_PAGES["faq"]
    content = render_doc_page(page["body"])
    return templates.TemplateResponse(
        "docs_page.html",
        {
            "request": request,
            "logged_in": logged_in,
            "page_title": page["title"],
            "page_slug": "faq",
            "content": content,
        },
    )
