"""Reverse proxy for deployed project subdomains."""

import logging

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, Response

from app.database import get_db

logger = logging.getLogger(__name__)


async def handle_subdomain_request(request: Request, project: str, username: str) -> Response:
    """Handle requests to {project}.{username}.justralph.it."""
    path = request.url.path.lstrip("/")

    # Look up project by username + project name
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT p.deploy_port, p.deploy_status FROM projects p "
            "JOIN users u ON p.user_id = u.id "
            "WHERE LOWER(u.github_username) = ? AND LOWER(p.name) = ?",
            (username.lower(), project.lower()),
        )
        row = await cursor.fetchone()

    if row is None or row["deploy_status"] != "running":
        return HTMLResponse(
            "<h1>Not deployed</h1><p>This project is not currently deployed.</p>",
            status_code=404,
        )

    deploy_port = row["deploy_port"]

    # Reverse proxy to the app's port
    target_url = f"http://127.0.0.1:{deploy_port}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("x-subdomain-project", None)
    headers.pop("x-subdomain-username", None)

    body = await request.body()

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers=dict(resp.headers),
            )
        except httpx.ConnectError:
            return HTMLResponse(
                "<h1>Service unavailable</h1><p>The app is not responding.</p>",
                status_code=502,
            )
        except httpx.TimeoutException:
            return HTMLResponse(
                "<h1>Gateway timeout</h1>",
                status_code=504,
            )
