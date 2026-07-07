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
                  decisions, and useful guidance in notes.
                - After each user answer, look for new unresolved branches that answer opens.
                  Capture those as open question notes when they matter for the project.
                - Before asking the user, call `read_notes` when existing notes may answer it.
                  Use `explore` when outside context can answer it. Do not ask questions
                  you can answer from notes, local context, or exploration.
                - Learn how much the user wants to control decisions. Record that as
                  ordinary notes when useful, and use it as soft guidance for how much
                  detail to ask about.
                - Treat missing product or technical detail as unresolved. Suggest options,
                  but do not decide product behavior, UX, scope, priority, stack,
                  architecture, or implementation details for the user.
                - If the user delegates any decision area, store the delegation boundary as
                  a decision and only decide within that boundary.
                - Use global notes only for project-wide facts. Create or use feature scopes
                  for feature-specific requirements, constraints, questions, and decisions.
                - Infer topic changes and call `switch_focus` when a different project area becomes active.
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
        "Read compact structured project notes. Returns human-readable summaries.",
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
        return self.notes.read(scope, kind, feature_id, ids, include_archived=include_archived)

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
        return self.notes.set_project(
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
        return self.notes.set_feature(feature_id, name, summary)

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
        return self.notes.add(kind, text, feature_id)

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
        return self.notes.resolve(question_id, decision_id, decision_text)

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
        return self.notes.revise(note_id, text)

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
        return self.notes.archive(note_id, reason)

    @tool(
        "Change the active discussion focus and rebuild context from structured notes.",
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
        if tool_name in {"explore", "read_notes"}:
            return

        tool_output = turn_items[-1:]
        if not tool_output:
            self.rebuild_context()
            return

        def read_field(item: ResponseInputItemParam, field_name: str) -> object:
            if isinstance(item, dict):
                return item.get(field_name)
            return getattr(item, field_name, None)

        call_id = read_field(tool_output[0], "call_id")
        for item in reversed(turn_items[:-1]):
            if read_field(item, "type") == "function_call" and read_field(item, "call_id") == call_id:
                tool_output.insert(0, item)
                break
        self.rebuild_context(recent_tail=tool_output)

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

        self._append_context_section(sections, self.notes.read("project", "all", None, None, include_archived=False))

        if focus.scope == "feature":
            self._append_context_section(
                sections, self.notes.read("global", "constraints", None, None, include_archived=False)
            )
            self._append_context_section(
                sections, self.notes.read("global", "decisions", None, None, include_archived=False)
            )
        else:
            self._append_context_section(sections, self.notes.read("global", "all", None, None, include_archived=False))

        if focus.scope == "feature" and focus.feature_id is not None:
            self._append_context_section(
                sections, self.notes.read("feature", "all", focus.feature_id, None, include_archived=False)
            )

        if focus.carry_ids:
            self._append_context_section(
                sections,
                "Carried context\n" + self.notes.read(None, None, None, focus.carry_ids, include_archived=False),
            )

        return "\n\n".join(sections)

    @staticmethod
    def _append_context_section(sections: list[str], section: str) -> None:
        if section and section != "No notes found.":
            sections.append(section)
