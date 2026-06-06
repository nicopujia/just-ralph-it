"""Tests for question tool data structures."""

from jri.core.interview import InterviewQuestion, QuestionChoice
from jri.core.tools.ask import AskChoice, build_question


def test_ask_builds_high_level_question() -> None:
    """High-level questions invite free-text answers."""
    assert build_question(
        level="high",
        question="What outcome would make this successful?",
    ) == InterviewQuestion(
        level="high",
        question="What outcome would make this successful?",
    )


def test_ask_builds_low_level_choices_with_default() -> None:
    """Low-level questions can show multiple-choice options."""
    assert build_question(
        level="low",
        question="When the API is unavailable, what should happen?",
        choices=[
            AskChoice(label="Retry for 30 seconds"),
            AskChoice(label="Show an error", description="Fail immediately."),
        ],
        default="Retry for 30 seconds",
    ) == InterviewQuestion(
        level="low",
        question="When the API is unavailable, what should happen?",
        choices=(
            QuestionChoice(label="Retry for 30 seconds"),
            QuestionChoice(
                label="Show an error",
                description="Fail immediately.",
            ),
        ),
        default="Retry for 30 seconds",
    )
