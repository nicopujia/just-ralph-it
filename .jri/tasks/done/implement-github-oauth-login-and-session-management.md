---
title: Implement GitHub OAuth login and session management
priority: 0
assignee: ralph
depends_on:
- create-sqlite-database-schema-and-initialization
created: '2026-03-21'
acceptance_criteria:
- Visiting /auth/login in a browser redirects to GitHub's OAuth page with the correct
  client_id and callback URL.
- After authorizing on GitHub, the browser is redirected to /auth/callback, which
  sets a `session` cookie and redirects to /dashboard.
- GET /auth/me returns JSON with the user's GitHub username and avatar URL.
- GET /auth/me without a session cookie returns
- 5. GET /auth/logout deletes the session cookie and redirects to /.
- The user's GitHub token, username, name, email, and avatar URL are stored in the
  SQLite users table.
- Logging in a second time updates the existing user row (upsert), not creating a
  duplicate.
- The session cookie is signed — tampering with it causes a 401 on /auth/me.
---

Implement GitHub OAuth in app/routers/auth.py and session middleware.

## OAuth Flow

### GET /auth/login
1. Generate a random `state` string (32 hex chars).
2. Store it in a temporary signed cookie (`oauth_state`, max_age=300 seconds).
3. Redirect to: `https://github.com/login/oauth/authorize?client_id={GITHUB_CLIENT_ID}&redirect_uri=https://justralph.it/auth/callback&scope=read:user,user:email&state={state}`

### GET /auth/callback?code=...&state=...
1. Verify `state` matches the `oauth_state` cookie. If not, return 400.
2. Exchange `code` for access token: POST `https://github.com/login/oauth/access_token` with client_id, client_secret, code. Accept: application/json.
3. Use the access token to GET `https://api.github.com/user` (with Authorization: Bearer header).
4. Upsert the user in SQLite: INSERT OR REPLACE into users table using github_id as the unique key. Store github_username, github_name, github_email, github_avatar_url, and the access_token (as github_token).
5. Set a signed cookie `session` containing the user's id (integer), using itsdangerous.URLSafeTimedSerializer with SECRET_KEY. Max age: 30 days.
6. Delete the `oauth_state` cookie.
7. Redirect to /dashboard.

### GET /auth/logout
1. Delete the `session` cookie.
2. Redirect to /.

### GET /auth/me (JSON API)
1. Read the `session` cookie, deserialize with itsdangerous (max_age=30 days).
2. Look up the user by id in SQLite.
3. Return JSON: {id, github_username, github_name, github_avatar_url}. 
4. If no valid session, return 401.

## Session middleware
Create a dependency `get_current_user(request: Request) -> dict` in app/auth_utils.py that:
1. Reads and deserializes the `session` cookie.
2. Queries the user from SQLite.
3. Returns the user row as a dict.
4. Raises HTTPException(401) if cookie is missing, expired, or user not found.

All protected endpoints use `Depends(get_current_user)`.

## HTTP client
Use httpx.AsyncClient for all HTTP calls to GitHub. Create a shared instance in config.py or use a lifespan-managed client.
