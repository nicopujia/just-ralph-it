from typing import Literal, override

from openai.types.responses import ResponseInputItemParam, ResponseInputParam

from jri.core.notes import Notes
from jri.core.settings import Settings

from .explorer import Explorer
from .shared import Agent, TextDelta, ToolCallStarted, tool


class Interviewer(Agent):
    """Interview the user and keep project notes current."""

    FIRST_MESSAGE = "What do you want to build?"

    def __init__(self, settings: Settings, notes: Notes) -> None:
        self.settings = settings
        self.notes = notes
        super().__init__(
            client=settings.llm_client,
            model=settings.interviewer_model,
            sys_prompt="""
                You are the Interviewer of the Just Ralph It (JRI) system,
                which is a tool to build any software project.

                Your task is to extract the full project idea that the user wants
                to build while keeping structured notes current.

                You only operate in two domains:
                - exploration, when useful context should be gathered with `explore`;
                - notes, when durable project understanding should be recorded.

                Rules:
                - Keep the user experience as normal chat. Never ask the user to manage notes, files, or context.
                - Record durable project facts, requirements, constraints, questions,
                  decisions, and user control/detail preferences in notes. Establish
                  `User control preference:` early when it is not already present, then
                  adapt question depth from that recorded answer.
                - Treat missing technical detail as unresolved. Do not invent stack,
                  architecture, or implementation decisions.
                - If the user delegates implementation choices, store the delegation boundary as a decision.
                - Use feature-local notes for feature-specific requirements, constraints, questions, and decisions.
                - Infer topic changes and call `switch_focus` internally when a different project area becomes active.
                - Prefer asking one focused question at a time unless the user asks for a broader pass.
            """,
            initial_ctx=self._build_context(),
        )

    @tool(
        "Gather context through a natural language query, including anything from the web or this computer.",
        running_label="Exploring",
        finished_label="Explored",
    )
    def explore(self, query: str) -> str:
        """Gather outside context for the current interview turn.

        Returns:
            The explorer's final streamed text output.
        """
        latest_output: list[str] = []
        for event in Explorer(self.settings).send_message(query):
            match event:
                case ToolCallStarted():
                    latest_output.clear()
                case TextDelta():
                    latest_output.append(event.text)
        return "".join(latest_output)

    @tool(
        "Read compact structured project notes. Returns human-readable summaries, not raw YAML.",
        running_label="Checking notes",
        finished_label="Checked notes",
    )
    def read_notes(
        self,
        scope: Literal["all", "project", "global", "feature"] | None,
        kind: Literal["all", "brief", "requirements", "constraints", "questions", "decisions", "features"] | None,
        feature_id: str | None,
        ids: list[str] | None,
        *,
        include_archived: bool | None,
    ) -> str:
        """Read structured notes as compact human-readable context.

        Returns:
            The rendered notes selected by the inputs.
        """
        return self.notes.read_notes(scope, kind, feature_id, ids, include_archived=include_archived)

    @tool(
        "Update the top-level project framing fields.", running_label="Updating notes", finished_label="Updated notes"
    )
    def set_project_brief(
        self,
        name: str | None,
        tldr: str | None,
        goal: str | None,
        target_user: str | None,
        success_outcome: str | None,
        software_type: str | None,
        codebase_status: str | None,
    ) -> str:
        """Update durable top-level project framing.

        Returns:
            A user-facing summary of changed fields.
        """
        return self.notes.set_project_brief(
            name=name,
            tldr=tldr,
            goal=goal,
            target_user=target_user,
            success_outcome=success_outcome,
            software_type=software_type,
            codebase_status=codebase_status,
        )

    @tool(
        "Create a new feature container for feature-local requirements, constraints, questions, and decisions.",
        running_label="Updating notes",
        finished_label="Updated notes",
    )
    def add_feature(self, name: str, summary: str) -> str:
        """Create a feature scope for local notes.

        Returns:
            A user-facing creation summary.
        """
        return self.notes.add_feature(name, summary)

    @tool("Rename or resummarize an existing feature.", running_label="Updating notes", finished_label="Updated notes")
    def set_feature_brief(self, feature_id: str, name: str | None, summary: str | None) -> str:
        """Update a feature name or summary.

        Returns:
            A user-facing summary of changed fields.
        """
        return self.notes.set_feature_brief(feature_id, name, summary)

    @tool(
        "Add one semantic project note without rewriting whole note sections.",
        running_label="Updating notes",
        finished_label="Updated notes",
    )
    def add_note(
        self, kind: Literal["requirement", "constraint", "question", "decision"], text: str, feature_id: str | None
    ) -> str:
        """Add one durable note to the global or feature scope.

        Returns:
            A user-facing creation summary.
        """
        return self.notes.add_note(kind, text, feature_id)

    @tool(
        "Mark a question resolved by linking it to an existing or newly created decision.",
        running_label="Updating notes",
        finished_label="Updated notes",
    )
    def resolve_question(self, question_id: str, decision_id: str | None, decision_text: str | None) -> str:
        """Resolve an open question with a decision.

        Returns:
            A user-facing resolution summary.
        """
        return self.notes.resolve_question(question_id, decision_id, decision_text)

    @tool(
        "Revise the text of an existing note without changing its identity.",
        running_label="Updating notes",
        finished_label="Updated notes",
    )
    def revise_note(self, note_id: str, text: str) -> str:
        """Revise an existing note while preserving its ID.

        Returns:
            A user-facing revision summary.
        """
        return self.notes.revise_note(note_id, text)

    @tool(
        "Archive an obsolete note with a reason instead of deleting it.",
        running_label="Updating notes",
        finished_label="Updated notes",
    )
    def archive_note(self, note_id: str, reason: str) -> str:
        """Archive a stale note and keep its history.

        Returns:
            A user-facing archive summary.
        """
        return self.notes.archive_note(note_id, reason)

    @tool(
        "Internally change the active discussion focus and rebuild context from structured notes.",
        running_label="Organizing notes",
        finished_label="Organized notes",
    )
    def switch_focus(
        self,
        scope: Literal["project", "global", "feature"],
        feature_id: str | None,
        carry_ids: list[str] | None,
        reason: str,
    ) -> str:
        """Change active focus and carried context.

        Returns:
            A user-facing focus summary.
        """
        return self.notes.switch_focus(scope, feature_id, carry_ids, reason)

    @override
    def after_tool_call(self, tool_name: str, turn_items: list[ResponseInputItemParam]) -> None:
        """Refresh surfaced notes context after note-mutating tools."""
        if tool_name not in {"explore", "read_notes"}:
            self.rebuild_context(recent_tail=turn_items)

    def rebuild_context(self, recent_tail: list[ResponseInputItemParam] | None = None) -> None:
        """Rebuild the active model context from durable notes."""
        self.reset_context([*self._build_context(), *(recent_tail or [])])

    def _build_context(self) -> ResponseInputParam:
        return [
            {"role": "system", "content": self._render_notes_context()},
            {"role": "assistant", "content": self.FIRST_MESSAGE},
        ]

    def _render_notes_context(self) -> str:
        focus = self.notes.state.focus
        sections = [
            (
                "Current structured notes context. Treat this as durable surfaced truth; "
                "if technical detail is missing, it is unresolved."
            ),
            f"Current focus: {focus.scope}"
            + (f" {focus.feature_id}" if focus.feature_id is not None else "")
            + f". Reason: {focus.reason}",
        ]

        self._append_context_section(
            sections, self.notes.read_notes("project", "all", None, None, include_archived=False)
        )

        if focus.scope == "global":
            self._append_context_section(
                sections, self.notes.read_notes("global", "all", None, None, include_archived=False)
            )
        else:
            self._append_context_section(
                sections, self.notes.read_notes("global", "constraints", None, None, include_archived=False)
            )
            self._append_context_section(
                sections, self.notes.read_notes("global", "decisions", None, None, include_archived=False)
            )

        if focus.scope == "feature" and focus.feature_id is not None:
            self._append_context_section(
                sections, self.notes.read_notes("feature", "all", focus.feature_id, None, include_archived=False)
            )

        if focus.carry_ids:
            self._append_context_section(
                sections,
                "Carried context\n" + self.notes.read_notes(None, None, None, focus.carry_ids, include_archived=False),
            )

        match self.notes.get_user_control_preference_state():
            case "resolved":
                pass
            case "open":
                sections.append(
                    "User control preference guidance\n"
                    "A global question marked `User control preference:` is open. Ask one lightweight question "
                    "when natural, then resolve it into a global decision before treating stack, architecture, "
                    "or question-depth preferences as settled."
                )
            case "absent":
                sections.append(
                    "User control preference guidance\n"
                    "No global question or decision marked `User control preference:` exists. Early in discovery, "
                    "create that global open question, ask whether the user wants to choose technical details, "
                    "approve proposals, or delegate those choices to JRI, then resolve it into a global decision."
                )

        return "\n\n".join(sections)

    @staticmethod
    def _append_context_section(sections: list[str], section: str) -> None:
        if section and section != "No notes found.":
            sections.append(section)
