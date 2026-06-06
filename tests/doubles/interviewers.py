"""Scripted interviewer doubles for integration tests."""

from collections.abc import AsyncIterator
from pathlib import Path

from jri.core.interview import InterviewEvent, InterviewQuestion
from jri.core.logging import JsonlLogger
from jri.core.tools.just_ralph_it import finalize_jri
from jri.core.tools.note import write_note
from jri.core.tools.spec import write_spec
from jri.core.triggers import is_trigger_message


class ScriptedInterviewer:
    """Deterministic interviewer that mirrors the live CLI contract."""

    def __init__(self, project_root: Path, logger: JsonlLogger) -> None:
        self.project_root: Path = project_root
        self.logger: JsonlLogger = logger
        self._goal_seen: bool = False
        self._ready: bool = False
        self._should_exit: bool = False

    @property
    def should_exit(self) -> bool:
        """Return whether finalization completed."""
        return self._should_exit

    async def respond(
        self,
        user_message: str,
    ) -> AsyncIterator[InterviewEvent]:
        """Respond with deterministic but product-shaped interview turns."""
        if is_trigger_message(user_message):
            if not self._ready:
                yield InterviewEvent(
                    kind="text",
                    content=(
                        "I can't finalize specs for Ralph handoff yet.\n"
                        "Missing: target user and success criteria."
                    ),
                )
                return

            yield InterviewEvent(kind="tool_call", content="just_ralph_it")
            result = await finalize_jri(
                project_root=self.project_root,
                latest_user_message=user_message,
                readiness_summary=(
                    "The scripted interview captured a goal, target user, "
                    "and first-version success criteria."
                ),
            )
            self._should_exit = result.should_exit
            yield InterviewEvent(kind="text", content=result.message)
            return

        if not self._goal_seen:
            self._goal_seen = True
            yield InterviewEvent(kind="tool_call", content="spec")
            await write_spec(
                project_root=self.project_root,
                path="product",
                content=(
                    f"# Product\n\n## Confirmed Goal\n\n- {user_message}\n"
                ),
            )
            yield InterviewEvent(kind="tool_call", content="note")
            await write_note(
                project_root=self.project_root,
                content=(
                    "# Scratchpad\n\n"
                    "## Open Topics\n\n"
                    "## Pending Questions\n\n"
                    "- Confirm target user and success criteria.\n\n"
                    "## Notes\n"
                ),
            )
            yield InterviewEvent(
                kind="question",
                content=InterviewQuestion(
                    level="high",
                    question=(
                        "Who is the primary user, and what would count as "
                        "success for the first version?"
                    ),
                ),
            )
            return

        self._ready = True
        yield InterviewEvent(kind="tool_call", content="spec")
        await write_spec(
            project_root=self.project_root,
            path="product",
            content=(
                "# Product\n\n"
                "## Confirmed Goal\n\n"
                "- Build a tiny CLI that prints hello.\n\n"
                "## Target User\n\n"
                f"- {user_message}\n\n"
                "## Success Criteria\n\n"
                "- Running the CLI prints hello to stdout.\n"
            ),
        )
        yield InterviewEvent(kind="tool_call", content="note")
        await write_note(
            project_root=self.project_root,
            content=(
                "# Scratchpad\n\n"
                "## Open Topics\n\n"
                "## Pending Questions\n\n"
                "## Notes\n\n"
                "- Ready for Ralph handoff after user trigger.\n"
            ),
        )
        yield InterviewEvent(
            kind="text",
            content=(
                "I have enough for the first-version behavior. "
                'Say "just ralph it" to finalize the specs.'
            ),
        )


def create_scripted_interviewer(
    project_root: Path,
    logger: JsonlLogger,
) -> ScriptedInterviewer:
    """Create the deterministic interviewer used by subprocess tests."""
    return ScriptedInterviewer(project_root=project_root, logger=logger)
