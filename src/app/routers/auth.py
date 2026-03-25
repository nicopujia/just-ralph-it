import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth_utils import SESSION_MAX_AGE, create_session_token, get_current_user
from app.config import BASE_URL, GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, SECRET_KEY
from app.database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])

_GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_USER_URL = "https://api.github.com/user"
_CALLBACK_URI = f"{BASE_URL}/auth/callback"


@router.get("/login")
async def login():
    state = secrets.token_hex(16)  # 32 hex chars
    params = urlencode(
        {
            "client_id": GITHUB_CLIENT_ID,
            "redirect_uri": _CALLBACK_URI,
            "scope": "read:user,user:email",
            "state": state,
        }
    )
    response = RedirectResponse(url=f"{_GITHUB_AUTHORIZE_URL}?{params}")
    response.set_cookie(
        "oauth_state",
        state,
        max_age=300,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@router.get("/callback")
async def callback(request: Request, code: str, state: str):
    # Verify state
    expected_state = request.cookies.get("oauth_state")
    if not expected_state or state != expected_state:
        return JSONResponse({"detail": "Invalid OAuth state"}, status_code=400)

    # Exchange code for access token
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            _GITHUB_TOKEN_URL,
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        token_data = token_resp.json()
        access_token = token_data.get("access_token")

        if not access_token:
            return JSONResponse(
                {"detail": "Failed to obtain access token"}, status_code=400
            )

        # Fetch GitHub user profile
        user_resp = await client.get(
            _GITHUB_USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        gh_user = user_resp.json()

    # Upsert user in SQLite
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO users (github_id, github_username, github_name,
                               github_email, github_avatar_url, github_token)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(github_id) DO UPDATE SET
                github_username = excluded.github_username,
                github_name = excluded.github_name,
                github_email = excluded.github_email,
                github_avatar_url = excluded.github_avatar_url,
                github_token = excluded.github_token
            """,
            (
                gh_user["id"],
                gh_user["login"],
                gh_user.get("name"),
                gh_user.get("email"),
                gh_user.get("avatar_url"),
                access_token,
            ),
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT id FROM users WHERE github_id = ?", (gh_user["id"],)
        )
        row = await cursor.fetchone()
        user_id = row["id"]

    # Set session cookie and redirect
    session_token = create_session_token(user_id)
    response = RedirectResponse(url="/projects", status_code=302)
    response.set_cookie(
        "session",
        session_token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    response.delete_cookie("oauth_state")
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("session")
    return response


ADMIN_USERNAME = "nicopujia"


@router.get("/impersonate/{username}")
async def impersonate(username: str, request: Request):
    """Log in as another user. Restricted to admin."""
    try:
        admin = await get_current_user(request)
    except Exception:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)

    if admin["github_username"] != ADMIN_USERNAME:
        return JSONResponse({"detail": "Forbidden"}, status_code=403)

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM users WHERE github_username = ?", (username,)
        )
        row = await cursor.fetchone()

    if not row:
        return JSONResponse({"detail": "User not found"}, status_code=404)

    session_token = create_session_token(row["id"])
    response = RedirectResponse(url="/projects", status_code=302)
    response.set_cookie(
        "session",
        session_token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@router.get("/me")
async def me(request: Request):
    try:
        user = await get_current_user(request)
    except Exception:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return {
        "id": user["id"],
        "github_username": user["github_username"],
        "github_name": user["github_name"],
        "github_avatar_url": user["github_avatar_url"],
    }
