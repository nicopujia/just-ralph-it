"""Freelist gating via user roles -- free/admin users skip Stripe payment."""

from app.schemas import User


def is_free_user(user: User) -> bool:
    """Return True if the user has a role that skips payment."""
    return user.role in ("admin", "free")
