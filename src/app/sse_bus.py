"""Centralized SSE event bus for real-time updates."""

import asyncio
from typing import Any


EVENT_TYPES = frozenset({
    "issue_update",
    "claude_md_update",
    "ralph_stdout",
    "ralph_status",
    "notification",
    "ralphy_processing",
})


class SSEBus:
    """In-process pub/sub bus keyed by project name."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, project_name: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.setdefault(project_name, set()).add(queue)
        return queue

    def unsubscribe(self, project_name: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(project_name)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                del self._subscribers[project_name]

    async def publish(self, project_name: str, event_type: str, data: dict[str, Any]) -> None:
        for queue in self._subscribers.get(project_name, set()).copy():
            try:
                queue.put_nowait({"event": event_type, "data": data})
            except asyncio.QueueFull:
                pass


# Singleton instance
sse_bus = SSEBus()
