"""Interviewer session interfaces."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class QuestionChoice:
    """One possible answer choice for a low-level question."""

    label: str
    description: str | None = None


@dataclass(frozen=True)
class InterviewQuestion:
    """One question the interviewer wants the user to answer next."""

    level: Literal["high", "low"]
    question: str
    choices: tuple[QuestionChoice, ...] = ()
    default: str | None = None


@dataclass(frozen=True)
class InterviewEvent:
    """One visible event from an interviewer turn."""

    kind: Literal["text", "text_delta", "tool_call", "question"]
    content: str | InterviewQuestion


class InterviewSession(Protocol):
    """Common interface for an interactive interview session."""

    @property
    def should_exit(self) -> bool:
        """Return whether the REPL should exit successfully."""
        ...

    def respond(self, user_message: str) -> AsyncIterator[InterviewEvent]:
        """Respond to one user message."""
        ...
