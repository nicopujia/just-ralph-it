"""Freelist gating via user roles -- free/admin users skip Stripe payment."""


def is_free_user(user: dict) -> bool:
    """Return True if the user has a role that skips payment."""
    return user.get("role") in ("admin", "free")
