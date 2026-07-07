from pathlib import Path
from typing import cast

from .helpers.ids import Kind, allocate_entry_id, allocate_feature_id, is_feature_id
from .models import AnyEntry, AnySection, Entry, Feature, FocusScope, FocusState, Question, ReadKind, ReadScope
from .storage import Storage

READ_ENTRY_KINDS: dict[ReadKind, Kind] = {
    "requirements": "requirement",
    "constraints": "constraint",
    "questions": "question",
    "decisions": "decision",
}


class Notes:
    """Service for structured project notes and focus state."""

    def __init__(self, document_file: Path, state_file: Path) -> None:
        self.storage = Storage(document_file, state_file)
        self.document = self.storage.load_document()
        self.state = self.storage.load_state(self.document)

    def read(
        self,
        scope: ReadScope | None,
        kind: ReadKind | None,
        feature_id: str | None,
        ids: list[str] | None,
        *,
        include_archived: bool | None,
    ) -> str:
        """Render selected entries as compact text for agents.

        Returns:
            Selected entries as compact text, or a not-found message.

        Raises:
            ValueError: If feature scope inputs are invalid.
        """
        selected_kind, selected_scope, include_hidden = kind or "all", scope or "all", include_archived is True
        lines: list[str] = []

        if ids:
            for entry_id in ids:
                if is_feature_id(entry_id):
                    feature = self.document.find_feature(entry_id)
                    lines.append(f"Feature {feature.id}: {feature.name}. {feature.summary}")
                    continue

                ref = self.document.find_entry(entry_id)
                if ref.entry.status == "archived" and not include_hidden:
                    continue
                lines.append(
                    f"{'Global' if ref.feature is None else f'Feature {ref.feature.id}'} "
                    f"{render_entry(ref.kind, ref.entry)}"
                )
            return "\n".join(lines).strip() or "No notes found."

        if feature_id is not None and selected_scope != "feature":
            raise ValueError("feature_id is only valid when scope is `feature`.")

        if selected_scope in {"all", "project"} and selected_kind in {"all", "brief"}:
            project = self.document.project
            title = project.name or "Untitled project"
            lines.append(f"# {title}")
            if project.tldr:
                lines.append(f"TL;DR: {project.tldr}")
            for label, value in [
                ("Goal", project.goal),
                ("Target user", project.target_user),
                ("Success outcome", project.success_outcome),
                ("Software type", project.software_type),
                ("Codebase status", project.codebase_status),
            ]:
                if value:
                    lines.append(f"- {label}: {value}")
            if len(lines) == 1 and title == "Untitled project":
                lines.append("Project brief: not set.")

        sections: list[tuple[str, AnySection]] = []
        if selected_scope in {"all", "global"}:
            sections.append(("Global", self.document.global_section))
        elif selected_scope != "project":
            if feature_id is None:
                raise ValueError("feature_id is required when scope is `feature`.")
            feature = self.document.find_feature(feature_id)
            if selected_kind in {"all", "brief"}:
                lines.append(f"Feature {feature.id}: {feature.name}. {feature.summary}")
            sections.append((f"Feature {feature.id}", feature))

        blocks: list[tuple[str, list[str]]] = []
        for scope_label, section in sections:
            for read_kind, entry_kind in READ_ENTRY_KINDS.items():
                if selected_kind in {"all", read_kind}:
                    block_lines = [
                        f"- {render_entry(entry_kind, entry)}"
                        for entry in section.entries(entry_kind)
                        if include_hidden or entry.status != "archived"
                    ]
                    if block_lines:
                        blocks.append((f"{scope_label} {read_kind}", block_lines))
        if (
            selected_scope == "all"
            and selected_kind in {"all", "features"}
            and (
                feature_lines := [
                    f"- {feature.id}: {feature.name} - {feature.summary}" for feature in self.document.features
                ]
            )
        ):
            blocks.append(("Features", feature_lines))

        for title, block_lines in blocks:
            if lines:
                lines.append("")
            lines.append(title)
            lines.extend(block_lines)

        return "\n".join(lines).strip() or "No notes found."

    def set_project(
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

        self.save()
        return f"Updated project brief: {', '.join(changed)}."

    def add_feature(self, name: str, summary: str) -> str:
        """Add a feature container.

        Returns:
            A user-facing creation summary.

        Raises:
            ValueError: If the name or summary is blank.
        """
        if not name.strip():
            raise ValueError("Feature name is required.")
        if not summary.strip():
            raise ValueError("Feature summary is required.")

        feature = Feature(
            id=allocate_feature_id(feature.id for feature in self.document.features), name=name, summary=summary
        )
        self.document.features.append(feature)
        self.save()
        return f"Added feature {feature.id}: {feature.name}"

    def set_feature(self, feature_id: str, name: str | None, summary: str | None) -> str:
        """Update a feature's name and summary fields.

        Returns:
            A user-facing summary of changed fields.

        Raises:
            ValueError: If the feature or replacement text is invalid.
        """
        feature = self.document.find_feature(feature_id)
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

        self.save()
        return f"Updated feature {feature.id}: {', '.join(changed)}."

    def add(self, kind: Kind, text: str, feature_id: str | None) -> str:
        """Add one entry to the global scope or a feature scope.

        Returns:
            A user-facing creation summary.

        Raises:
            ValueError: If the entry text or feature scope is invalid.
        """
        if not text.strip():
            raise ValueError("Note text is required.")

        section: AnySection = (
            self.document.global_section if feature_id is None else self.document.find_feature(feature_id)
        )
        entry_id = allocate_entry_id(kind, (entry.id for entry in section.entries(kind)), feature_id)
        if kind == "question":
            entry = Question(id=entry_id, text=text)
            label = "open question"
        else:
            entry = Entry(id=entry_id, text=text)
            label = kind
        section.entries(kind).append(entry)

        self.save()
        return f"Added {label} {entry_id}: {text}"

    def resolve(self, question_id: str, decision_id: str | None, decision_text: str | None) -> str:
        """Resolve an open question with an existing or new decision.

        Returns:
            A user-facing resolution summary.

        Raises:
            ValueError: If the question or decision inputs are invalid.
        """
        if (decision_id is None) == (decision_text is None):
            raise ValueError("Provide exactly one of decision_id or decision_text.")

        question_ref = self.document.find_question(question_id)
        question = cast("Question", question_ref.entry)
        if question.status != "open":
            raise ValueError(f"Question `{question_id}` must be open to resolve.")

        section: AnySection = self.document.global_section if question_ref.feature is None else question_ref.feature
        if decision_id is not None:
            decision_ref = self.document.find_decision(decision_id)
            if decision_ref.feature is not question_ref.feature:
                raise ValueError("Resolved questions must link to a decision in the same scope.")
            if decision_ref.entry.status != "active":
                raise ValueError(f"Decision `{decision_id}` must be active.")
            decision = cast("Entry", decision_ref.entry)
        else:
            if decision_text is None or not decision_text.strip():
                raise ValueError("decision_text is required when decision_id is not provided.")
            decision_feature_id = question_ref.feature.id if question_ref.feature else None
            decision = Entry(
                id=allocate_entry_id(
                    "decision", (entry.id for entry in section.entries("decision")), decision_feature_id
                ),
                text=decision_text,
            )
            section.entries("decision").append(decision)

        question.status = "resolved"
        question.decision = decision.id
        self.save()
        return f"Resolved question {question.id} with decision {decision.id}: {decision.text}"

    def revise(self, entry_id: str, text: str) -> str:
        """Revise entry text without changing its ID.

        Returns:
            A user-facing revision summary.

        Raises:
            ValueError: If the entry ID or replacement text is invalid.
        """
        if not text.strip():
            raise ValueError("Note text is required.")
        ref = self.document.find_entry(entry_id)
        ref.entry.text = text
        self.save()
        return f"Revised {ref.kind} {ref.entry.id}: {text}"

    def archive(self, entry_id: str, reason: str) -> str:
        """Archive an entry and remove it from carried focus if needed.

        Returns:
            A user-facing archive summary.

        Raises:
            ValueError: If the entry ID or archive reason is invalid.
        """
        if not reason.strip():
            raise ValueError("Archive reason is required.")
        ref = self.document.find_entry(entry_id)
        if ref.kind == "decision":
            section: AnySection = self.document.global_section if ref.feature is None else ref.feature
            blocking_question_ids = [
                question.id
                for question in section.questions
                if question.status == "resolved" and question.decision == entry_id
            ]
            if blocking_question_ids:
                question_ids = ", ".join(blocking_question_ids)
                raise ValueError(f"Decision `{entry_id}` resolves active question(s): {question_ids}.")
        ref.entry.status = "archived"
        ref.entry.archive_reason = reason
        removed_from_focus = entry_id in self.state.focus.carry_ids
        if removed_from_focus:
            self.state.focus.carry_ids = [carry_id for carry_id in self.state.focus.carry_ids if carry_id != entry_id]
        self.save()
        if removed_from_focus:
            self.save_state()
            return f"Archived {ref.kind} {ref.entry.id}: {reason}. Removed from carried focus context."
        return f"Archived {ref.kind} {ref.entry.id}: {reason}"

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
            feature = self.document.find_feature(feature_id)
        else:
            if feature_id is not None:
                raise ValueError("feature_id is only valid for feature focus.")
            feature = None

        valid_carry_ids = carry_ids or []
        for carry_id in valid_carry_ids:
            self.document.ensure_carry(carry_id)

        self.state.focus = FocusState(scope=scope, feature_id=feature_id, carry_ids=valid_carry_ids, reason=reason)
        self.save_state()

        target = f"feature {feature.id}: {feature.name}" if feature else scope
        carried = ", ".join(valid_carry_ids) if valid_carry_ids else "none"
        return f"Switched focus to {target}. Carrying: {carried}."

    def save(self) -> None:
        """Validate and write the structured YAML document."""
        self.storage.save_document(self.document)

    def save_state(self) -> None:
        """Validate and write runtime state JSON."""
        self.storage.save_state(self.document, self.state)


def render_entry(kind: Kind, entry: AnyEntry) -> str:
    """Render one compact entry line.

    Returns:
        A compact entry line with status metadata.
    """
    rendered = f"{entry.id}: {entry.text}"
    if kind == "question":
        question = cast("Question", entry)
        decision = f", decision: {question.decision}" if question.decision else ""
        rendered += f" ({question.status}{decision})"
    elif entry.status != "active":
        rendered += f" ({entry.status})"
    if entry.archive_reason:
        rendered += f" Reason: {entry.archive_reason}"
    return rendered
