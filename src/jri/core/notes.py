import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, NamedTuple, Self, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ActiveStatus = Literal["active", "archived"]
QuestionStatus = Literal["open", "resolved", "archived"]
NoteKind = Literal["requirement", "constraint", "question", "decision"]
ReadScope = Literal["all", "project", "global", "feature"]
ReadKind = Literal["all", "brief", "requirements", "constraints", "questions", "decisions", "features"]
FocusScope = Literal["project", "global", "feature"]

_FEATURE_ID_PATTERN = re.compile(r"f([1-9][0-9]*)")
_GLOBAL_ID_PATTERNS = {
    "requirement": re.compile(r"r([1-9][0-9]*)"),
    "constraint": re.compile(r"c([1-9][0-9]*)"),
    "question": re.compile(r"q([1-9][0-9]*)"),
    "decision": re.compile(r"d([1-9][0-9]*)"),
}
_NOTE_PREFIXES: dict[NoteKind, str] = {"requirement": "r", "constraint": "c", "question": "q", "decision": "d"}


class ProjectBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    tldr: str | None = None
    goal: str | None = None
    target_user: str | None = None
    success_outcome: str | None = None
    software_type: str | None = None
    codebase_status: str | None = None


class TrackedNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    status: ActiveStatus = "active"
    archive_reason: str | None = None

    @model_validator(mode="after")
    def validate_archive_reason(self) -> Self:
        if self.status == "archived" and not self.archive_reason:
            raise ValueError(f"Archived note `{self.id}` must store archive_reason.")
        if self.status != "archived" and self.archive_reason is not None:
            raise ValueError(f"Active note `{self.id}` must not store archive_reason.")
        return self


class QuestionNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    status: QuestionStatus = "open"
    decision: str | None = None
    archive_reason: str | None = None

    @model_validator(mode="after")
    def validate_status_fields(self) -> Self:
        if self.status == "open" and self.decision is not None:
            raise ValueError(f"Open question `{self.id}` must not link to a decision.")
        if self.status == "resolved" and not self.decision:
            raise ValueError(f"Resolved question `{self.id}` must link to a decision.")
        if self.status == "archived" and not self.archive_reason:
            raise ValueError(f"Archived question `{self.id}` must store archive_reason.")
        if self.status != "archived" and self.archive_reason is not None:
            raise ValueError(f"Question `{self.id}` must not store archive_reason unless archived.")
        return self


class NotesSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirements: list[TrackedNote] = Field(default_factory=list)
    constraints: list[TrackedNote] = Field(default_factory=list)
    questions: list[QuestionNote] = Field(default_factory=list)
    decisions: list[TrackedNote] = Field(default_factory=list)


class FeatureNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    summary: str
    requirements: list[TrackedNote] = Field(default_factory=list)
    constraints: list[TrackedNote] = Field(default_factory=list)
    questions: list[QuestionNote] = Field(default_factory=list)
    decisions: list[TrackedNote] = Field(default_factory=list)


class NotesDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project: ProjectBrief = Field(default_factory=ProjectBrief)
    global_notes: NotesSection = Field(default_factory=NotesSection, alias="global")
    features: list[FeatureNotes] = Field(default_factory=list)


class FocusState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: FocusScope = "project"
    feature_id: str | None = None
    carry_ids: list[str] = Field(default_factory=list)
    reason: str = "Initial project discovery."

    @model_validator(mode="after")
    def validate_feature_scope(self) -> Self:
        if self.scope == "feature" and not self.feature_id:
            raise ValueError("Feature focus requires feature_id.")
        if self.scope != "feature" and self.feature_id is not None:
            raise ValueError("Only feature focus may store feature_id.")
        return self


class RuntimeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    focus: FocusState = Field(default_factory=FocusState)


type AnyNote = TrackedNote | QuestionNote
type AnySection = NotesSection | FeatureNotes


class NoteRef(NamedTuple):
    kind: NoteKind
    note: AnyNote
    feature: FeatureNotes | None


class Notes:
    def __init__(self, notes_file: Path, state_file: Path) -> None:
        self.notes_file = notes_file
        self.state_file = state_file
        self.notes_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.document = self._load_notes()
        self.state = self._load_state()

    def read_notes(  # noqa: C901, PLR0912
        self,
        scope: ReadScope | None,
        kind: ReadKind | None,
        feature_id: str | None,
        ids: list[str] | None,
        *,
        include_archived: bool | None,
    ) -> str:
        include = include_archived is True
        selected_kind = kind or "all"

        if ids:
            return self._render_ids(ids, include_archived=include)

        selected_scope = scope or "all"
        if feature_id is not None and selected_scope != "feature":
            raise ValueError("feature_id is only valid when scope is `feature`.")

        lines: list[str] = []
        match selected_scope:
            case "all":
                if selected_kind in {"all", "brief"}:
                    self._render_project_brief(lines)
                if selected_kind in {"all", "requirements", "constraints", "questions", "decisions"}:
                    self._render_section(
                        lines, "Global", self.document.global_notes, selected_kind, include_archived=include
                    )
                if selected_kind in {"all", "features"}:
                    self._render_features(lines)
            case "project":
                if selected_kind in {"all", "brief"}:
                    self._render_project_brief(lines)
                if selected_kind in {"all", "features"}:
                    self._render_features(lines)
            case "global":
                self._render_section(
                    lines, "Global", self.document.global_notes, selected_kind, include_archived=include
                )
            case "feature":
                if feature_id is None:
                    raise ValueError("feature_id is required when scope is `feature`.")
                feature = self._get_feature(feature_id)
                if selected_kind in {"all", "brief"}:
                    lines.append(f"Feature {feature.id}: {feature.name}. {feature.summary}")
                self._render_section(lines, f"Feature {feature.id}", feature, selected_kind, include_archived=include)

        return "\n".join(lines).strip() or "No notes found."

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
        if not name.strip():
            raise ValueError("Feature name is required.")
        if not summary.strip():
            raise ValueError("Feature summary is required.")

        feature = FeatureNotes(id=self._next_feature_id(), name=name, summary=summary)
        self.document.features.append(feature)
        self.save_notes()
        return f"Added feature {feature.id}: {feature.name}"

    def set_feature_brief(self, feature_id: str, name: str | None, summary: str | None) -> str:
        feature = self._get_feature(feature_id)
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
        if not text.strip():
            raise ValueError("Note text is required.")

        section: AnySection = self.document.global_notes if feature_id is None else self._get_feature(feature_id)
        note_id = self._next_note_id(kind, section, feature_id)
        if kind == "question":
            note = QuestionNote(id=note_id, text=text)
            section.questions.append(note)
            label = "open question"
        else:
            note = TrackedNote(id=note_id, text=text)
            self._notes_for_kind(section, kind).append(note)
            label = kind

        self.save_notes()
        return f"Added {label} {note_id}: {text}"

    def resolve_question(self, question_id: str, decision_id: str | None, decision_text: str | None) -> str:
        if (decision_id is None) == (decision_text is None):
            raise ValueError("Provide exactly one of decision_id or decision_text.")

        question_ref = self._find_note(question_id)
        if question_ref.kind != "question" or not isinstance(question_ref.note, QuestionNote):
            raise ValueError(f"`{question_id}` is not a question ID.")
        question = question_ref.note
        if question.status != "open":
            raise ValueError(f"Question `{question_id}` must be open to resolve.")

        section: AnySection = self.document.global_notes if question_ref.feature is None else question_ref.feature
        if decision_id is not None:
            decision_ref = self._find_note(decision_id)
            if decision_ref.kind != "decision" or not isinstance(decision_ref.note, TrackedNote):
                raise ValueError(f"`{decision_id}` is not a decision ID.")
            if decision_ref.feature is not question_ref.feature:
                raise ValueError("Resolved questions must link to a decision in the same scope.")
            if decision_ref.note.status != "active":
                raise ValueError(f"Decision `{decision_id}` must be active.")
            decision = decision_ref.note
        else:
            if decision_text is None or not decision_text.strip():
                raise ValueError("decision_text is required when decision_id is not provided.")
            decision_feature_id = question_ref.feature.id if question_ref.feature else None
            decision = TrackedNote(id=self._next_note_id("decision", section, decision_feature_id), text=decision_text)
            section.decisions.append(decision)

        question.status = "resolved"
        question.decision = decision.id
        self.save_notes()
        return f"Resolved question {question.id} with decision {decision.id}: {decision.text}"

    def revise_note(self, note_id: str, text: str) -> str:
        if not text.strip():
            raise ValueError("Note text is required.")
        ref = self._find_note(note_id)
        ref.note.text = text
        self.save_notes()
        return f"Revised {ref.kind} {ref.note.id}: {text}"

    def archive_note(self, note_id: str, reason: str) -> str:
        if not reason.strip():
            raise ValueError("Archive reason is required.")
        ref = self._find_note(note_id)
        ref.note.status = "archived"
        ref.note.archive_reason = reason
        self.save_notes()
        return f"Archived {ref.kind} {ref.note.id}: {reason}"

    def switch_focus(self, scope: FocusScope, feature_id: str | None, carry_ids: list[str] | None, reason: str) -> str:
        if not reason.strip():
            raise ValueError("Focus switch reason is required.")
        if scope == "feature":
            if feature_id is None:
                raise ValueError("feature_id is required for feature focus.")
            feature = self._get_feature(feature_id)
        else:
            if feature_id is not None:
                raise ValueError("feature_id is only valid for feature focus.")
            feature = None

        valid_carry_ids = carry_ids or []
        for carry_id in valid_carry_ids:
            self._validate_carry_id(carry_id)

        self.state.focus = FocusState(scope=scope, feature_id=feature_id, carry_ids=valid_carry_ids, reason=reason)
        self.save_state()

        target = f"feature {feature.id}: {feature.name}" if feature else scope
        carried = ", ".join(valid_carry_ids) if valid_carry_ids else "none"
        return f"Switched focus to {target}. Carrying: {carried}."

    def save_notes(self) -> None:
        self._validate_document(self.document)
        data = self.document.model_dump(by_alias=True, mode="json", exclude_none=True)
        data["project"] = self.document.project.model_dump(mode="json")
        self.notes_file.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    def save_state(self) -> None:
        self._validate_state(self.state)
        self.state_file.write_text(f"{json.dumps(self.state.model_dump(mode='json'), indent=2)}\n")

    def _load_notes(self) -> NotesDocument:
        if not self.notes_file.exists() or not self.notes_file.read_text().strip():
            document = NotesDocument()
            self.document = document
            self.save_notes()
            return document

        try:
            data = yaml.safe_load(self.notes_file.read_text())
        except yaml.YAMLError as error:
            raise RuntimeError(f"Malformed notes.yaml: {error}") from error

        if not isinstance(data, dict):
            raise TypeError("Malformed notes.yaml: expected a YAML mapping.")

        try:
            document = NotesDocument.model_validate(data)
        except ValidationError as error:
            raise RuntimeError(f"Malformed notes.yaml: {error}") from error

        self._validate_document(document)
        return document

    def _load_state(self) -> RuntimeState:
        if not self.state_file.exists() or not self.state_file.read_text().strip():
            state = RuntimeState()
            self.state = state
            self.save_state()
            return state

        try:
            data: Any = json.loads(self.state_file.read_text())
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Malformed .jri/state.json: {error}") from error

        try:
            state = RuntimeState.model_validate(data)
        except ValidationError as error:
            raise RuntimeError(f"Malformed .jri/state.json: {error}") from error

        self._validate_state(state)
        return state

    def _validate_document(self, document: NotesDocument) -> None:
        seen_feature_ids: set[str] = set()
        for feature in document.features:
            if not _FEATURE_ID_PATTERN.fullmatch(feature.id):
                raise RuntimeError(f"Malformed notes.yaml: invalid feature ID `{feature.id}`.")
            if feature.id in seen_feature_ids:
                raise RuntimeError(f"Malformed notes.yaml: duplicate feature ID `{feature.id}`.")
            seen_feature_ids.add(feature.id)

        self._validate_section(document.global_notes, None)
        for feature in document.features:
            self._validate_section(feature, feature.id)

    def _validate_section(self, section: AnySection, feature_id: str | None) -> None:
        seen_ids: set[str] = set()
        decisions = {decision.id for decision in section.decisions}

        for kind, note in self._iter_section_notes(section):
            if note.id in seen_ids:
                raise RuntimeError(f"Malformed notes.yaml: duplicate note ID `{note.id}`.")
            seen_ids.add(note.id)
            self._validate_note_id(kind, note.id, feature_id)
            if isinstance(note, QuestionNote) and note.status == "resolved":
                if note.decision not in decisions:
                    raise RuntimeError(
                        f"Malformed notes.yaml: resolved question `{note.id}` links to missing "
                        f"decision `{note.decision}`."
                    )
                self._validate_note_id("decision", cast("str", note.decision), feature_id)

    @staticmethod
    def _validate_note_id(kind: NoteKind, note_id: str, feature_id: str | None) -> None:
        local_id = note_id
        if feature_id is not None:
            prefix = f"{feature_id}/"
            if not note_id.startswith(prefix):
                raise RuntimeError(f"Malformed notes.yaml: `{note_id}` must be scoped under `{feature_id}`.")
            local_id = note_id.removeprefix(prefix)
        elif "/" in note_id:
            raise RuntimeError(f"Malformed notes.yaml: global note ID `{note_id}` must not contain `/`.")

        if not _GLOBAL_ID_PATTERNS[kind].fullmatch(local_id):
            raise RuntimeError(f"Malformed notes.yaml: invalid {kind} ID `{note_id}`.")

    def _validate_state(self, state: RuntimeState) -> None:
        if state.focus.feature_id is not None:
            self._get_feature(state.focus.feature_id)
        for carry_id in state.focus.carry_ids:
            self._validate_carry_id(carry_id)

    def _validate_carry_id(self, carry_id: str) -> None:
        if _FEATURE_ID_PATTERN.fullmatch(carry_id):
            self._get_feature(carry_id)
            return
        ref = self._find_note(carry_id)
        if ref.note.status == "archived":
            raise ValueError(f"Archived note `{carry_id}` cannot be carried into focus.")

    def _get_feature(self, feature_id: str) -> FeatureNotes:
        for feature in self.document.features:
            if feature.id == feature_id:
                return feature
        raise ValueError(f"Unknown feature ID `{feature_id}`.")

    def _find_note(self, note_id: str) -> NoteRef:
        for kind, note in self._iter_section_notes(self.document.global_notes):
            if note.id == note_id:
                return NoteRef(kind, note, None)
        for feature in self.document.features:
            for kind, note in self._iter_section_notes(feature):
                if note.id == note_id:
                    return NoteRef(kind, note, feature)
        raise ValueError(f"Unknown note ID `{note_id}`.")

    @staticmethod
    def _iter_section_notes(section: AnySection) -> list[tuple[NoteKind, AnyNote]]:
        return [
            *(("requirement", note) for note in section.requirements),
            *(("constraint", note) for note in section.constraints),
            *(("question", note) for note in section.questions),
            *(("decision", note) for note in section.decisions),
        ]

    @staticmethod
    def _notes_for_kind(
        section: AnySection, kind: Literal["requirement", "constraint", "decision"]
    ) -> list[TrackedNote]:
        match kind:
            case "requirement":
                return section.requirements
            case "constraint":
                return section.constraints
            case "decision":
                return section.decisions

    def _next_feature_id(self) -> str:
        highest = 0
        for feature in self.document.features:
            if match := _FEATURE_ID_PATTERN.fullmatch(feature.id):
                highest = max(highest, int(match.group(1)))
        return f"f{highest + 1}"

    def _next_note_id(self, kind: NoteKind, section: AnySection, feature_id: str | None) -> str:
        prefix = _NOTE_PREFIXES[kind]
        highest = 0
        for note in self._notes_for_allocated_kind(section, kind):
            local_id = note.id.removeprefix(f"{feature_id}/") if feature_id is not None else note.id
            if match := re.fullmatch(rf"{prefix}([1-9][0-9]*)", local_id):
                highest = max(highest, int(match.group(1)))
        note_id = f"{prefix}{highest + 1}"
        return f"{feature_id}/{note_id}" if feature_id is not None else note_id

    @staticmethod
    def _notes_for_allocated_kind(section: AnySection, kind: NoteKind) -> Sequence[AnyNote]:
        match kind:
            case "requirement":
                return section.requirements
            case "constraint":
                return section.constraints
            case "question":
                return section.questions
            case "decision":
                return section.decisions

    def _render_ids(self, ids: list[str], *, include_archived: bool) -> str:
        lines: list[str] = []
        for note_id in ids:
            if _FEATURE_ID_PATTERN.fullmatch(note_id):
                feature = self._get_feature(note_id)
                lines.append(f"Feature {feature.id}: {feature.name}. {feature.summary}")
                continue

            ref = self._find_note(note_id)
            if ref.note.status == "archived" and not include_archived:
                continue
            scope = "Global" if ref.feature is None else f"Feature {ref.feature.id}"
            lines.append(f"{scope} {self._render_note(ref.kind, ref.note)}")

        return "\n".join(lines).strip() or "No notes found."

    def _render_project_brief(self, lines: list[str]) -> None:
        project = self.document.project
        title = project.name or "Untitled project"
        lines.append(f"# {title}")
        if project.tldr:
            lines.append(f"TL;DR: {project.tldr}")

        fields = [
            ("Goal", project.goal),
            ("Target user", project.target_user),
            ("Success outcome", project.success_outcome),
            ("Software type", project.software_type),
            ("Codebase status", project.codebase_status),
        ]
        for label, value in fields:
            if value:
                lines.append(f"- {label}: {value}")
        if len(lines) == 1 and title == "Untitled project":
            lines.append("Project brief: not set.")

    def _render_features(self, lines: list[str]) -> None:
        if not self.document.features:
            return
        if lines:
            lines.append("")
        lines.append("Features")
        lines.extend(f"- {feature.id}: {feature.name} - {feature.summary}" for feature in self.document.features)

    def _render_section(
        self, lines: list[str], scope_label: str, section: AnySection, kind: ReadKind, *, include_archived: bool
    ) -> None:
        if kind in {"all", "requirements"}:
            self._render_note_group(
                lines,
                f"{scope_label} requirements",
                "requirement",
                section.requirements,
                include_archived=include_archived,
            )
        if kind in {"all", "constraints"}:
            self._render_note_group(
                lines,
                f"{scope_label} constraints",
                "constraint",
                section.constraints,
                include_archived=include_archived,
            )
        if kind in {"all", "questions"}:
            self._render_note_group(
                lines, f"{scope_label} questions", "question", section.questions, include_archived=include_archived
            )
        if kind in {"all", "decisions"}:
            self._render_note_group(
                lines, f"{scope_label} decisions", "decision", section.decisions, include_archived=include_archived
            )

    def _render_note_group(
        self,
        lines: list[str],
        title: str,
        kind: NoteKind,
        notes: list[TrackedNote] | list[QuestionNote],
        *,
        include_archived: bool,
    ) -> None:
        visible_notes = [note for note in notes if include_archived or note.status != "archived"]
        if not visible_notes:
            return
        if lines:
            lines.append("")
        lines.append(title)
        lines.extend(f"- {self._render_note(kind, note)}" for note in visible_notes)

    @staticmethod
    def _render_note(kind: NoteKind, note: AnyNote) -> str:
        rendered = f"{note.id}: {note.text}"
        if kind == "question" and isinstance(note, QuestionNote):
            rendered += f" ({note.status}"
            if note.decision:
                rendered += f", decision: {note.decision}"
            rendered += ")"
        elif note.status != "active":
            rendered += f" ({note.status})"
        if note.archive_reason:
            rendered += f" Reason: {note.archive_reason}"
        return rendered
