from fastapi import HTTPException, Request
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.config import SECRET_KEY
from app.database import get_db

SESSION_MAX_AGE = 30 * 24 * 60 * 60  # 30 days in seconds
_serializer = URLSafeTimedSerializer(SECRET_KEY)


def create_session_token(
    user_id: int, *, impersonating_from: int | None = None
) -> str:
    """Sign a user id into a session token.

    If ``impersonating_from`` is provided, it is stored alongside the
    user id so the admin can return to their own account later.
    """
    payload: dict = {"uid": user_id}
    if impersonating_from is not None:
        payload["ifrom"] = impersonating_from
    return _serializer.dumps(payload)


def decode_session_token(token: str) -> dict:
    """Decode and verify a session token.

    Returns a dict with ``uid`` (int) and optionally ``ifrom`` (int).
    Handles legacy tokens that stored a bare int.
    """
    data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
    # Legacy tokens are a bare int
    if isinstance(data, int):
        return {"uid": data}
    return data


async def get_current_user(request: Request) -> dict:
    """Read session cookie, look up user in SQLite, return user dict.

    Raises HTTPException(401) if the session is missing, expired,
    tampered with, or the user no longer exists.
    """
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        session_data = decode_session_token(token)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    user_id = session_data["uid"]

    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=401, detail="User not found")

    user = dict(row)
    if "ifrom" in session_data:
        user["impersonating_from"] = session_data["ifrom"]
    return user
