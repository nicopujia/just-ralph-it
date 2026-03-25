"""Reverse proxy for deployed project subdomains."""

import logging
from pathlib import Path

import httpx
from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from app.database import get_db

logger = logging.getLogger(__name__)

STATIC_SITES_DIR = Path("/var/www/jri-sites")


async def handle_subdomain_request(request: Request, subdomain: str) -> Response:
    """Handle requests to {subdomain}.justralph.it."""
    path = request.url.path.lstrip("/")

    # Look up project
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT deploy_type, deploy_port, deploy_status FROM projects WHERE deploy_subdomain = ?",
            (subdomain,),
        )
        row = await cursor.fetchone()

    if row is None or row["deploy_status"] != "running":
        return HTMLResponse(
            "<h1>Not deployed</h1><p>This project is not currently deployed.</p>",
            status_code=404,
        )

    deploy_type = row["deploy_type"]
    deploy_port = row["deploy_port"]

    if deploy_type == "static":
        # Serve static files
        site_dir = STATIC_SITES_DIR / subdomain
        if not site_dir.exists():
            return HTMLResponse("<h1>Not found</h1>", status_code=404)

        file_path = site_dir / path if path else site_dir / "index.html"
        if file_path.is_dir():
            file_path = file_path / "index.html"
        if not file_path.exists():
            # Try .html extension
            html_path = file_path.with_suffix(".html")
            if html_path.exists():
                file_path = html_path
            else:
                # SPA fallback
                file_path = site_dir / "index.html"
        if file_path.exists() and site_dir in file_path.resolve().parents or file_path.resolve() == site_dir:
            return FileResponse(file_path)
        return HTMLResponse("<h1>Not found</h1>", status_code=404)

    elif deploy_type == "dynamic":
        # Reverse proxy to the app's port
        target_url = f"http://127.0.0.1:{deploy_port}/{path}"
        if request.url.query:
            target_url += f"?{request.url.query}"

        headers = dict(request.headers)
        headers.pop("host", None)
        headers.pop("x-subdomain", None)

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

    return HTMLResponse("<h1>Not found</h1>", status_code=404)
