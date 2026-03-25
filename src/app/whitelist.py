"""Closed-beta gating via user roles."""

from fastapi import HTTPException

from app.config import DATA_DIR


def check_whitelist(user: dict) -> None:
    """Raise 403 if user lacks beta access. Adds them to waitlist."""
    if user.get("role") in ("admin", "beta", "free"):
        return

    github_username: str = user["github_username"]
    waitlist_path = DATA_DIR / "waitlist.txt"

    existing = (
        set(waitlist_path.read_text().splitlines()) if waitlist_path.exists() else set()
    )
    if github_username not in existing:
        with open(waitlist_path, "a") as f:
            f.write(f"{github_username}\n")

    raise HTTPException(
        status_code=403,
        detail="We're in closed beta. We'll let you know when you're in.",
    )
