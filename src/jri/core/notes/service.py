from pathlib import Path

from .finders import find_decision, find_note, find_question, get_feature, list_notes
from .ids import allocate_feature_id, allocate_note_id
from .models import (
    AnySection,
    FeatureNotes,
    FocusScope,
    FocusState,
    NoteKind,
    QuestionNote,
    ReadKind,
    ReadScope,
    TrackedNote,
)
from .persistence import load_notes, load_state
from .persistence import save_notes as persist_notes
from .persistence import save_state as persist_state
from .readers import get_user_control_preference_state as query_user_control_preference_state
from .readers import read_notes as query_read_notes
from .validation import validate_carry_id


class Notes:
    """Domain service for structured project notes and focus state."""

    def __init__(self, notes_file: Path, state_file: Path) -> None:
        self.notes_file = notes_file
        self.state_file = state_file
        self.notes_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.document = load_notes(self.notes_file)
        self.state = load_state(self.state_file, self.document)

    def read_notes(
        self,
        scope: ReadScope | None,
        kind: ReadKind | None,
        feature_id: str | None,
        ids: list[str] | None,
        *,
        include_archived: bool | None,
    ) -> str:
        """Render selected notes as compact text for agents.

        Returns:
            The selected notes as compact text, or a not-found message.
        """
        return query_read_notes(self.document, scope, kind, feature_id, ids, include_archived=include_archived)

    def get_user_control_preference_state(self) -> str:
        """Return user-control guidance status.

        Returns:
            Whether the preference marker is absent, open, or resolved.
        """
        return query_user_control_preference_state(self.document)

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
        """Update non-empty project brief fields.

        Returns:
            A user-facing summary of changed fields.
        """
        updates = {
            "name": name,
            "tldr": tldr,
            "goal": goal,
            "target_user": target_user,
            "success_outcome": success_outcome,
            "software_type": software_type,
            "codebase_status": codebase_status,
        }
        changed: list[str] = []
        for field_name, value in updates.items():
            if value is None:
                continue
            setattr(self.document.project, field_name, value)
            changed.append(field_name)

        if not changed:
            return "Project brief unchanged."

        self.save_notes()
        return f"Updated project brief: {', '.join(changed)}."

    def add_feature(self, name: str, summary: str) -> str:
        """Add a feature notes container.

        Returns:
            A user-facing creation summary.

        Raises:
            ValueError: If the name or summary is blank.
        """
        if not name.strip():
            raise ValueError("Feature name is required.")
        if not summary.strip():
            raise ValueError("Feature summary is required.")

        feature = FeatureNotes(
            id=allocate_feature_id(feature.id for feature in self.document.features), name=name, summary=summary
        )
        self.document.features.append(feature)
        self.save_notes()
        return f"Added feature {feature.id}: {feature.name}"

    def set_feature_brief(self, feature_id: str, name: str | None, summary: str | None) -> str:
        """Update a feature's name and summary fields.

        Returns:
            A user-facing summary of changed fields.

        Raises:
            ValueError: If the feature or replacement text is invalid.
        """
        feature = get_feature(self.document, feature_id)
        changed: list[str] = []
        if name is not None:
            if not name.strip():
                raise ValueError("Feature name is required.")
            feature.name = name
            changed.append("name")
        if summary is not None:
            if not summary.strip():
                raise ValueError("Feature summary is required.")
            feature.summary = summary
            changed.append("summary")
        if not changed:
            return f"Feature {feature.id} unchanged."

        self.save_notes()
        return f"Updated feature {feature.id}: {', '.join(changed)}."

    def add_note(self, kind: NoteKind, text: str, feature_id: str | None) -> str:
        """Add one note to the global scope or a feature scope.

        Returns:
            A user-facing creation summary.

        Raises:
            ValueError: If the note text or feature scope is invalid.
        """
        if not text.strip():
            raise ValueError("Note text is required.")

        section: AnySection = (
            self.document.global_notes if feature_id is None else get_feature(self.document, feature_id)
        )
        note_id = allocate_note_id(kind, (note.id for note in list_notes(section, kind)), feature_id)
        if kind == "question":
            note = QuestionNote(id=note_id, text=text)
            label = "open question"
        else:
            note = TrackedNote(id=note_id, text=text)
            label = kind
        list_notes(section, kind).append(note)

        self.save_notes()
        return f"Added {label} {note_id}: {text}"

    def resolve_question(self, question_id: str, decision_id: str | None, decision_text: str | None) -> str:
        """Resolve an open question with an existing or new decision.

        Returns:
            A user-facing resolution summary.

        Raises:
            ValueError: If the question or decision inputs are invalid.
        """
        if (decision_id is None) == (decision_text is None):
            raise ValueError("Provide exactly one of decision_id or decision_text.")

        question_ref = find_question(self.document, question_id)
        question = question_ref.note
        if question.status != "open":
            raise ValueError(f"Question `{question_id}` must be open to resolve.")

        section: AnySection = self.document.global_notes if question_ref.feature is None else question_ref.feature
        if decision_id is not None:
            decision_ref = find_decision(self.document, decision_id)
            if decision_ref.feature is not question_ref.feature:
                raise ValueError("Resolved questions must link to a decision in the same scope.")
            if decision_ref.note.status != "active":
                raise ValueError(f"Decision `{decision_id}` must be active.")
            decision = decision_ref.note
        else:
            if decision_text is None or not decision_text.strip():
                raise ValueError("decision_text is required when decision_id is not provided.")
            decision_feature_id = question_ref.feature.id if question_ref.feature else None
            decision = TrackedNote(
                id=allocate_note_id(
                    "decision", (note.id for note in list_notes(section, "decision")), decision_feature_id
                ),
                text=decision_text,
            )
            list_notes(section, "decision").append(decision)

        question.status = "resolved"
        question.decision = decision.id
        self.save_notes()
        return f"Resolved question {question.id} with decision {decision.id}: {decision.text}"

    def revise_note(self, note_id: str, text: str) -> str:
        """Revise note text without changing its ID.

        Returns:
            A user-facing revision summary.

        Raises:
            ValueError: If the note ID or replacement text is invalid.
        """
        if not text.strip():
            raise ValueError("Note text is required.")
        ref = find_note(self.document, note_id)
        ref.note.text = text
        self.save_notes()
        return f"Revised {ref.kind} {ref.note.id}: {text}"

    def archive_note(self, note_id: str, reason: str) -> str:
        """Archive a note and remove it from carried focus if needed.

        Returns:
            A user-facing archive summary.

        Raises:
            ValueError: If the note ID or archive reason is invalid.
        """
        if not reason.strip():
            raise ValueError("Archive reason is required.")
        ref = find_note(self.document, note_id)
        if ref.kind == "decision":
            section: AnySection = self.document.global_notes if ref.feature is None else ref.feature
            blocking_question_ids = [
                question.id
                for question in section.questions
                if question.status == "resolved" and question.decision == note_id
            ]
            if blocking_question_ids:
                question_ids = ", ".join(blocking_question_ids)
                raise ValueError(f"Decision `{note_id}` resolves active question(s): {question_ids}.")
        ref.note.status = "archived"
        ref.note.archive_reason = reason
        removed_from_focus = note_id in self.state.focus.carry_ids
        if removed_from_focus:
            self.state.focus.carry_ids = [carry_id for carry_id in self.state.focus.carry_ids if carry_id != note_id]
        self.save_notes()
        if removed_from_focus:
            self.save_state()
            return f"Archived {ref.kind} {ref.note.id}: {reason}. Removed from carried focus context."
        return f"Archived {ref.kind} {ref.note.id}: {reason}"

    def switch_focus(self, scope: FocusScope, feature_id: str | None, carry_ids: list[str] | None, reason: str) -> str:
        """Persist the active interview focus.

        Returns:
            A user-facing focus summary.

        Raises:
            ValueError: If focus inputs are invalid.
        """
        if not reason.strip():
            raise ValueError("Focus switch reason is required.")
        if scope == "feature":
            if feature_id is None:
                raise ValueError("feature_id is required for feature focus.")
            feature = get_feature(self.document, feature_id)
        else:
            if feature_id is not None:
                raise ValueError("feature_id is only valid for feature focus.")
            feature = None

        valid_carry_ids = carry_ids or []
        for carry_id in valid_carry_ids:
            validate_carry_id(self.document, carry_id)

        self.state.focus = FocusState(scope=scope, feature_id=feature_id, carry_ids=valid_carry_ids, reason=reason)
        self.save_state()

        target = f"feature {feature.id}: {feature.name}" if feature else scope
        carried = ", ".join(valid_carry_ids) if valid_carry_ids else "none"
        return f"Switched focus to {target}. Carrying: {carried}."

    def save_notes(self) -> None:
        """Validate and write notes YAML."""
        persist_notes(self.notes_file, self.document)

    def save_state(self) -> None:
        """Validate and write runtime state JSON."""
        persist_state(self.state_file, self.document, self.state)
