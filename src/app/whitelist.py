"""Closed-beta whitelist gating."""

from fastapi import HTTPException

from app.config import DATA_DIR


def check_whitelist(user: dict) -> None:
    """Raise 403 if user is not whitelisted. Adds them to waitlist."""
    github_username: str = user["github_username"]
    whitelist_path = DATA_DIR / "whitelist.txt"
    waitlist_path = DATA_DIR / "waitlist.txt"

    whitelisted = set()
    if whitelist_path.exists():
        whitelisted = {
            line.strip()
            for line in whitelist_path.read_text().splitlines()
            if line.strip()
        }

    if github_username in whitelisted:
        return

    # Append to waitlist if not already in either list
    existing = set(waitlist_path.read_text().splitlines()) if waitlist_path.exists() else set()
    if github_username not in existing and github_username not in whitelisted:
        with open(waitlist_path, "a") as f:
            f.write(f"{github_username}\n")

    raise HTTPException(
        status_code=403,
        detail="Closed beta. Your username has been added to the waitlist.",
    )
