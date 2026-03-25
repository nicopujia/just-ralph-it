"""SSE streaming endpoint for real-time project events."""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.auth_utils import get_current_user
from app.database import get_db
from app.sse_bus import sse_bus

router = APIRouter(prefix="/api/projects", tags=["sse"])

KEEPALIVE_INTERVAL = 30  # seconds


@router.get("/{project_name}/events")
async def project_events(project_name: str, user: dict = Depends(get_current_user)):
    """Stream SSE events for a project.

    Returns text/event-stream. Sends a keepalive comment every 30 seconds
    and cleans up the subscription when the client disconnects.
    """

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM projects WHERE user_id = ? AND name = ?",
            (user["id"], project_name),
        )
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")

    async def _stream():
        queue = sse_bus.subscribe(project_name)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_INTERVAL)
                    yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
                except asyncio.TimeoutError:
                    # Keepalive comment to prevent connection timeout
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            sse_bus.unsubscribe(project_name, queue)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
