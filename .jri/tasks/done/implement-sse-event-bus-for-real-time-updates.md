---
title: Implement SSE event bus for real-time updates
priority: 0
assignee: ralph
depends_on:
- initialize-python-project-with-fastapi-uvicorn-and-project-structure
created: '2026-03-21'
acceptance_criteria:
- GET /api/projects/{name}/events returns a text/event-stream response.
- When Ralph publishes a stdout line, connected SSE clients receive it as a ralph_stdout
  event.
- When an issue status changes, connected SSE clients receive an issue_update event.
- When CLAUDE.md changes, connected SSE clients receive an claude_md_update event.
- SSE connection sends keepalive comments every 30 seconds to prevent timeout.
- Multiple SSE clients can connect to the same project and all receive the same events.
- Disconnecting a client cleans up the subscription (no memory leak).
- The background poller runs every 3 seconds for projects with active Ralph loops.
- The SSE bus is a singleton — all parts of the app publish to the same instance.
---

Create a centralized SSE event bus in app/sse_bus.py that the frontend connects to for all real-time updates.

## GET /api/projects/{name}/events (in app/routers/sse.py)
- Requires auth. Verify project belongs to user.
- Returns text/event-stream.
- The client connects once and receives ALL event types for this project:
  - `issue_update`: an issue was created, updated, or its status changed
  - `claude_md_update`: CLAUDE.md was modified
  - `ralph_stdout`: a line of Ralph's output
  - `ralph_status`: Ralph loop status changed (started, stopped, done, crash_recovery)
  - `notification`: a notification for the user (Ralph needs help)
  - `ralphy_processing`: Ralphy started/finished processing (for disabling input)

## app/sse_bus.py

### Class: SSEBus
A singleton per-project event bus.

```python
class SSEBus:
    def __init__(self):
        self._subscribers: dict[int, set[asyncio.Queue]] = {}  # project_id -> set of queues
    
    def subscribe(self, project_id: int) -> asyncio.Queue:
        queue = asyncio.Queue(maxsize=100)
        self._subscribers.setdefault(project_id, set()).add(queue)
        return queue
    
    def unsubscribe(self, project_id: int, queue: asyncio.Queue):
        self._subscribers.get(project_id, set()).discard(queue)
    
    async def publish(self, project_id: int, event_type: str, data: dict):
        for queue in self._subscribers.get(project_id, set()):
            try:
                queue.put_nowait({"event": event_type, "data": data})
            except asyncio.QueueFull:
                pass  # Drop old events if client is slow

sse_bus = SSEBus()  # Singleton
```

### SSE endpoint implementation
```python
@router.get('/api/projects/{name}/events')
async def project_events(name: str, user=Depends(get_current_user)):
    project = await get_project(user, name)
    queue = sse_bus.subscribe(project['id'])
    
    async def event_generator():
        try:
            while True:
                event = await asyncio.wait_for(queue.get(), timeout=30)
                yield f'event: {event["event"]}\ndata: {json.dumps(event["data"])}\n\n'
        except asyncio.TimeoutError:
            yield f': keepalive\n\n'  # Send keepalive comment
        except asyncio.CancelledError:
            pass
        finally:
            sse_bus.unsubscribe(project['id'], queue)
    
    return StreamingResponse(event_generator(), media_type='text/event-stream')
```

### Integration points
- The RalphLoop publishes ralph_stdout and ralph_status events.
- The chat endpoint publishes ralphy_processing events (start/end).
- A background polling task publishes issue_update events when bd state changes.
- The notifications system publishes notification events.

### Background issue poller
Create a background task that, for each project with an active Ralph loop, polls `bd list --json` every 3 seconds and compares with the previous state. If any issue's status changed or new issues appeared, publish issue_update events. Also poll CLAUDE.md content (hash comparison) and publish claude_md_update if changed.
