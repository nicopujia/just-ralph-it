from fastapi import HTTPException, Request
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.config import SECRET_KEY
from app.database import get_db

SESSION_MAX_AGE = 30 * 24 * 60 * 60  # 30 days in seconds
_serializer = URLSafeTimedSerializer(SECRET_KEY)


def create_session_token(user_id: int) -> str:
    """Sign a user id into a session token."""
    return _serializer.dumps(user_id)


def decode_session_token(token: str) -> int:
    """Decode and verify a session token. Returns user id."""
    return _serializer.loads(token, max_age=SESSION_MAX_AGE)


async def get_current_user(request: Request) -> dict:
    """Read session cookie, look up user in SQLite, return user dict.

    Raises HTTPException(401) if the session is missing, expired,
    tampered with, or the user no longer exists.
    """
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        user_id = decode_session_token(token)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=401, detail="User not found")

    return dict(row)
