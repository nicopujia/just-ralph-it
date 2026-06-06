"""Tests for terminal rendering helpers."""

from jri.cli.rendering import render_question
from jri.core.interview import InterviewQuestion, QuestionChoice


def test_render_question_displays_question_text_without_level_label() -> None:
    """Questions render without exposing interview planning labels."""
    assert (
        render_question(
            InterviewQuestion(
                level="high",
                question="What outcome would make this successful?",
            )
        )
        == "What outcome would make this successful?"
    )


def test_render_question_displays_choices_without_level_label() -> None:
    """Multiple-choice questions render only question and options."""
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
        "When the API is unavailable, what should happen?\n"
        "\n"
        "Options:\n"
        "A. Retry for 30 seconds (default)\n"
        "B. Show an error - Fail immediately."
    )
