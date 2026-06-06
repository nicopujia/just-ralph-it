"""Scripted interviewer doubles for integration tests."""

from collections.abc import AsyncIterator
from pathlib import Path

from jri.core.interview import InterviewEvent, InterviewQuestion
from jri.core.logging import JsonlLogger
from jri.core.tools.just_ralph_it import finalize_jri
from jri.core.tools.note import replace_note
from jri.core.tools.spec import replace_spec
from jri.core.triggers import is_trigger_message

HIDDEN_SPEC_PHRASE = "unique hidden spec phrase: citrus ledger 7842"


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
                        "I can't finalize specs yet.\n"
                        "Missing: target user and success criteria."
                    ),
                )
                return

            yield InterviewEvent(kind="tool_call", content="finalize_specs")
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
            yield InterviewEvent(kind="tool_call", content="update_specs")
            await replace_spec(
                project_root=self.project_root,
                path="product",
                content=(
                    f"# Product\n\n## Confirmed Goal\n\n- {user_message}\n"
                ),
            )
            yield InterviewEvent(kind="tool_call", content="record_notes")
            await replace_note(
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
        yield InterviewEvent(kind="tool_call", content="update_specs")
        await replace_spec(
            project_root=self.project_root,
            path="product",
            content=(
                "# Product\n\n"
                "## Confirmed Goal\n\n"
                "- Build a tiny CLI that prints hello.\n\n"
                "## Target User\n\n"
                f"- {user_message}\n\n"
                "## Workflows\n\n"
                "- The user runs the CLI command once.\n\n"
                "## Inputs\n\n"
                "- No arguments and no stdin are required.\n\n"
                "## Outputs\n\n"
                "- The CLI prints hello to stdout.\n\n"
                "## Persistence\n\n"
                "- No data is persisted.\n\n"
                "## Integrations\n\n"
                "- No external services are used.\n\n"
                "## Errors\n\n"
                "- No custom error behavior is needed for v1.\n\n"
                "## Edge Cases\n\n"
                "- Extra arguments may be ignored.\n\n"
                "## Non-Goals\n\n"
                "- Packaging, colors, prompts, network, files, and "
                "deployment are out of scope.\n\n"
                "## Success Criteria\n\n"
                "- Running the CLI prints hello to stdout.\n"
            ),
        )
        yield InterviewEvent(kind="tool_call", content="record_notes")
        await replace_note(
            project_root=self.project_root,
            content=(
                "# Scratchpad\n\n"
                "## Open Topics\n\n"
                "## Pending Questions\n\n"
                "## Notes\n\n"
                "- Ready to finalize after user trigger.\n"
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


class FirstTokenInterviewer:
    """Interviewer that streams a contraction as the first text delta."""

    @property
    def should_exit(self) -> bool:
        """Return false so EOF controls the test session."""
        return False

    async def respond(
        self,
        user_message: str,
    ) -> AsyncIterator[InterviewEvent]:
        """Yield text deltas whose first token must not be dropped."""
        _ = user_message
        yield InterviewEvent(kind="text_delta", content="I'm")
        yield InterviewEvent(kind="text_delta", content=" checking")
        yield InterviewEvent(kind="text_delta", content=" the first token.")


def create_first_token_interviewer(
    project_root: Path,
    logger: JsonlLogger,
) -> FirstTokenInterviewer:
    """Create a deterministic first-token regression interviewer."""
    _ = (project_root, logger)
    return FirstTokenInterviewer()


class HiddenSpecInterviewer:
    """Interviewer that persists exact specs without dumping them."""

    def __init__(self, project_root: Path) -> None:
        self.project_root: Path = project_root

    @property
    def should_exit(self) -> bool:
        """Return false so EOF controls the test session."""
        return False

    async def respond(
        self,
        user_message: str,
    ) -> AsyncIterator[InterviewEvent]:
        """Persist exact spec text, revealing it only on direct request."""
        if _asks_for_exact_specs(user_message):
            spec_path = self.project_root / ".jri" / "specs" / "product.md"
            yield InterviewEvent(
                kind="text",
                content=spec_path.read_text(encoding="utf-8"),
            )
            return

        yield InterviewEvent(kind="tool_call", content="update_specs")
        await replace_spec(
            project_root=self.project_root,
            path="product",
            content=(
                "# Product\n\n"
                "## Confirmed Goal\n\n"
                f"- Persist {HIDDEN_SPEC_PHRASE} for the product spec.\n"
            ),
        )
        yield InterviewEvent(
            kind="text",
            content="I captured the exact wording without dumping the spec.",
        )


def create_hidden_spec_interviewer(
    project_root: Path,
    logger: JsonlLogger,
) -> HiddenSpecInterviewer:
    """Create a deterministic hidden-spec interviewer."""
    _ = logger
    return HiddenSpecInterviewer(project_root)


def _asks_for_exact_specs(user_message: str) -> bool:
    normalized = user_message.casefold()
    return "exact" in normalized and "spec" in normalized


class NonSoftwareInterviewer:
    """Interviewer that keeps non-software input conversational."""

    @property
    def should_exit(self) -> bool:
        """Return false so EOF controls the test session."""
        return False

    async def respond(
        self,
        user_message: str,
    ) -> AsyncIterator[InterviewEvent]:
        """Answer conversationally without writing specs."""
        _ = user_message
        yield InterviewEvent(
            kind="text",
            content=(
                "That sounds conversational rather than a software project, "
                "so I will not finalize specs from it."
            ),
        )


def create_non_software_interviewer(
    project_root: Path,
    logger: JsonlLogger,
) -> NonSoftwareInterviewer:
    """Create a deterministic non-software interviewer."""
    _ = (project_root, logger)
    return NonSoftwareInterviewer()
