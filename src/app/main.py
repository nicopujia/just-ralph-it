import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.config import DATA_DIR
from app.database import init_db
from app.logging_config import setup_logging
from app.routers import auth, pages, projects, chat, ralph, uploads, sse

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    await init_db()
    yield


app = FastAPI(title="Just Ralph It", lifespan=lifespan)

# Increase multipart upload limit (default is 1MB, we allow 3MB files)
from starlette.formparsers import MultiPartParser
MultiPartParser.max_file_size = 1024 * 1024 * 10  # 10MB

# Mount static files
_static_dir = Path(__file__).resolve().parent.parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Include routers
app.include_router(pages.router)
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(chat.router)
app.include_router(ralph.router)
app.include_router(ralph.pricing_router)
app.include_router(uploads.router)
app.include_router(sse.router)


from starlette.types import ASGIApp, Receive, Scope, Send


class SubdomainMiddleware:
    """Route subdomain requests without wrapping responses (avoids StreamingResponse issues)."""
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            project = headers.get(b"x-subdomain-project", b"").decode()
            username = headers.get(b"x-subdomain-username", b"").decode()
            if project and username:
                from app.routers.deploy_proxy import handle_subdomain_request
                from starlette.requests import Request as StarletteRequest
                request = StarletteRequest(scope, receive, send)
                response = await handle_subdomain_request(request, project, username)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(SubdomainMiddleware)
