---
title: Add authentication to SSE endpoint
priority: 0
assignee: Nicolás Pujia
created: '2026-03-21'
acceptance_criteria:
- GET /api/projects/{name}/events returns 401 if no session cookie
- GET /api/projects/{name}/events returns 404 if user doesn't own the project
- Authenticated users can still subscribe to their own project's events
- The comment about auth being added later is removed
---

The SSE endpoint at /api/projects/{project_name}/events in app/routers/sse.py has NO authentication. Any unauthenticated client can subscribe to any project's real-time events including issue updates, ralph stdout, and notifications. There is even a comment on line 24 that says 'Auth will be added by a later task'.

WHAT TO CHANGE in app/routers/sse.py:

1. Add imports:
   from fastapi import APIRouter, Depends, HTTPException
   from app.auth_utils import get_current_user
   from app.database import get_db

2. Change the endpoint signature (line 17):
   FROM: async def project_events(project_name: str):
   TO:   async def project_events(project_name: str, user: dict = Depends(get_current_user)):

3. Add project ownership verification after getting the user:
   async with get_db() as db:
       cursor = await db.execute(
           'SELECT id FROM projects WHERE user_id = ? AND name = ?',
           (user['id'], project_name),
       )
       row = await cursor.fetchone()
   if row is None:
       raise HTTPException(status_code=404, detail='Project not found')

4. Remove the comment about auth being added later.

NOTE: SSE uses EventSource in the browser which sends cookies automatically, so the session cookie will be included. No frontend changes needed.
