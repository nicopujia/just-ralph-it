"""Question tool data structures."""

from typing import Literal

from pydantic import BaseModel

from jri.core.interview import InterviewQuestion, QuestionChoice


class AskChoice(BaseModel):
    """One possible answer choice for a low-level question."""

    label: str
    description: str | None = None


def build_question(
    *,
    level: Literal["high", "low"],
    question: str,
    choices: list[AskChoice] | None = None,
    default: str | None = None,
) -> InterviewQuestion:
    """Build a structured question for the next REPL turn."""
    return InterviewQuestion(
        level=level,
        question=question,
        choices=tuple(
            QuestionChoice(
                label=choice.label,
                description=choice.description,
            )
            for choice in choices or []
        ),
        default=default,
    )
