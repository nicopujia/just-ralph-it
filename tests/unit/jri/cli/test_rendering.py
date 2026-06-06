"""Tests for terminal rendering helpers."""

from jri.cli.rendering import render_question
from jri.core.interview import InterviewQuestion, QuestionChoice


def test_render_question_displays_high_level_question() -> None:
    """High-level questions render as terminal text."""
    assert render_question(
        InterviewQuestion(
            level="high",
            question="What outcome would make this successful?",
        )
    ) == ("High-level question:\nWhat outcome would make this successful?")


def test_render_question_displays_low_level_choices_with_default() -> None:
    """Low-level questions render multiple-choice options."""
    assert render_question(
        InterviewQuestion(
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
    ) == (
        "Low-level question:\n"
        "When the API is unavailable, what should happen?\n"
        "\n"
        "Options:\n"
        "A. Retry for 30 seconds (default)\n"
        "B. Show an error - Fail immediately."
    )
