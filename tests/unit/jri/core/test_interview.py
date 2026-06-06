"""Tests for interview session data structures."""

from jri.core.interview import (
    InterviewEvent,
    InterviewQuestion,
    QuestionChoice,
)


def test_interview_question_defaults_to_no_choices() -> None:
    """Questions can be created without choices or defaults."""
    question = InterviewQuestion(level="high", question="Who is this for?")

    assert question.choices == ()
    assert question.default is None


def test_interview_event_accepts_structured_questions() -> None:
    """Question events carry the structured question object."""
    question = InterviewQuestion(
        level="low",
        question="Which interface?",
        choices=(QuestionChoice(label="CLI", description="Terminal"),),
        default="CLI",
    )

    event = InterviewEvent(kind="question", content=question)

    assert event.content == question
