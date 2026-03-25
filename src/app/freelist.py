"""Freelist gating -- users in freelist.txt skip Stripe payment."""

from app.config import DATA_DIR


def is_free_user(user: dict) -> bool:
    """Return True if the user's GitHub username is in freelist.txt."""
    github_username: str = user["github_username"]
    freelist_path = DATA_DIR / "freelist.txt"

    if not freelist_path.exists():
        return False

    free_users = {
        line.strip()
        for line in freelist_path.read_text().splitlines()
        if line.strip()
    }
    return github_username in free_users
