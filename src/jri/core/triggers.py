"""Finalization trigger phrase detection."""

_TRIGGERS = {"just ralph it", "ralph it", "jri", "ralfealo"}


def is_trigger_message(message: str) -> bool:
    """Return whether a message is exactly a finalization trigger."""
    normalized = " ".join(message.casefold().strip().split())
    if normalized.startswith("please "):
        normalized = normalized.removeprefix("please ").strip()
    if normalized.endswith(" please"):
        normalized = normalized.removesuffix(" please").strip()
    return normalized in _TRIGGERS
