from typing import Literal

from openai.types.responses import ResponseInputItemParam, ResponseInputParam

from jri.core.notes import Notes
from jri.core.settings import Settings

from .explorer import Explorer
from .shared import Agent, TextDelta, ToolCallStarted, tool


class Interviewer(Agent):
    FIRST_MESSAGE = "What do you want to build?"

    def __init__(self, settings: Settings, notes: Notes) -> None:
        self.settings = settings
        self.notes = notes
        super().__init__(
            client=settings.llm_client,
            model=settings.interviewer_model,
            sys_prompt=self._system_prompt(),
            initial_ctx=self._build_context(),
        )

    @tool("Gather context through a natural language query, including anything from the web or this computer.")
    def explore(self, query: str) -> str:
        latest_output: list[str] = []
        for event in Explorer(self.settings).send_message(query):
            match event:
                case ToolCallStarted():
                    latest_output.clear()
                case TextDelta():
                    latest_output.append(event.text)
        return "".join(latest_output)

    @tool("Read compact structured project notes. Returns human-readable summaries, not raw YAML.")
    def read_notes(
        self,
        scope: Literal["all", "project", "global", "feature"] | None,
        kind: Literal["all", "brief", "requirements", "constraints", "questions", "decisions", "features"] | None,
        feature_id: str | None,
        ids: list[str] | None,
        *,
        include_archived: bool | None,
    ) -> str:
        return self.notes.read_notes(scope, kind, feature_id, ids, include_archived=include_archived)

    @tool("Update the top-level project framing fields.")
    def set_project_brief(  # noqa: PLR0913, PLR0917
        self,
        name: str | None,
        tldr: str | None,
        goal: str | None,
        target_user: str | None,
        success_outcome: str | None,
        software_type: str | None,
        codebase_status: str | None,
    ) -> str:
        return self.notes.set_project_brief(
            name=name,
            tldr=tldr,
            goal=goal,
            target_user=target_user,
            success_outcome=success_outcome,
            software_type=software_type,
            codebase_status=codebase_status,
        )

    @tool("Create a new feature container for feature-local requirements, constraints, questions, and decisions.")
    def add_feature(self, name: str, summary: str) -> str:
        return self.notes.add_feature(name, summary)

    @tool("Rename or resummarize an existing feature.")
    def set_feature_brief(self, feature_id: str, name: str | None, summary: str | None) -> str:
        return self.notes.set_feature_brief(feature_id, name, summary)

    @tool("Add one semantic project note without rewriting whole note sections.")
    def add_note(
        self, kind: Literal["requirement", "constraint", "question", "decision"], text: str, feature_id: str | None
    ) -> str:
        return self.notes.add_note(kind, text, feature_id)

    @tool("Mark a question resolved by linking it to an existing or newly created decision.")
    def resolve_question(self, question_id: str, decision_id: str | None, decision_text: str | None) -> str:
        return self.notes.resolve_question(question_id, decision_id, decision_text)

    @tool("Revise the text of an existing note without changing its identity.")
    def revise_note(self, note_id: str, text: str) -> str:
        return self.notes.revise_note(note_id, text)

    @tool("Archive an obsolete note with a reason instead of deleting it.")
    def archive_note(self, note_id: str, reason: str) -> str:
        return self.notes.archive_note(note_id, reason)

    @tool("Internally change the active discussion focus and rebuild context from structured notes.")
    def switch_focus(
        self,
        scope: Literal["project", "global", "feature"],
        feature_id: str | None,
        carry_ids: list[str] | None,
        reason: str,
    ) -> str:
        return self.notes.switch_focus(scope, feature_id, carry_ids, reason)

    def after_tool_call(self, tool_name: str, turn_items: list[ResponseInputItemParam]) -> None:
        if tool_name == "switch_focus":
            self.rebuild_context(recent_tail=turn_items)

    def rebuild_context(self, recent_tail: list[ResponseInputItemParam] | None = None) -> None:
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

        return "\n\n".join(sections)

    @staticmethod
    def _append_context_section(sections: list[str], section: str) -> None:
        if section and section != "No notes found.":
            sections.append(section)

    @staticmethod
    def _system_prompt() -> str:
        return """
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
              decisions, and user control/detail preferences in notes.
            - Treat missing technical detail as unresolved. Do not invent stack,
              architecture, or implementation decisions.
            - If the user delegates implementation choices, store the delegation boundary as a decision.
            - Use feature-local notes for feature-specific requirements, constraints, questions, and decisions.
            - Infer topic changes and call `switch_focus` internally when a different project area becomes active.
            - Prefer asking one focused question at a time unless the user asks for a broader pass.
        """
