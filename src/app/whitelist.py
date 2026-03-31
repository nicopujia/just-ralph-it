"""Closed-beta gating via user roles."""

from fastapi import HTTPException

from app.config import DATA_DIR
from app.schemas import User


def check_whitelist(user: User) -> None:
    """Raise 403 if user lacks beta access. Adds them to waitlist."""
    if user.role in ("admin", "beta", "free"):
        return

    waitlist_path = DATA_DIR / "waitlist.txt"

    existing = (
        set(waitlist_path.read_text().splitlines()) if waitlist_path.exists() else set()
    )
    if user.github_username not in existing:
        with open(waitlist_path, "a") as f:
            f.write(f"{user.github_username}\n")

    raise HTTPException(
        status_code=403,
        detail="We're in closed beta. We'll let you know when you're in.",
    )
