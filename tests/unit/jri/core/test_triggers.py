"""Tests for conservative finalization trigger detection."""

import pytest

from jri.core.triggers import is_trigger_message


@pytest.mark.parametrize(
    "message",
    [
        "just ralph it",
        "please just ralph it",
        "ralph it please",
        "jri",
        "ralfealo",
    ],
)
def test_trigger_detection_accepts_exact_trigger_phrases(
    message: str,
) -> None:
    """Exact finalization triggers are recognized."""
    assert is_trigger_message(message)


@pytest.mark.parametrize(
    "message",
    [
        "What does JRI mean?",
        "I think the JRI folder should be hidden.",
        "Can you explain just ralph it first?",
        "Do not ralph it yet.",
    ],
)
def test_trigger_detection_rejects_accidental_mentions(message: str) -> None:
    """Incidental trigger phrase mentions do not finalize."""
    assert not is_trigger_message(message)
