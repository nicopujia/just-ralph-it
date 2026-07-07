from collections.abc import Iterator
from typing import Any, Literal, NamedTuple, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .helpers.ids import Kind, is_feature_id, validate_entry_id

type ActiveStatus = Literal["active", "archived"]
type QuestionStatus = Literal["open", "resolved", "archived"]
type ReadScope = Literal["all", "project", "global", "feature"]
type ReadKind = Literal["all", "brief", "requirements", "constraints", "questions", "decisions", "features"]
type FocusScope = Literal["project", "global", "feature"]
type AnySection = Section | Feature
type AnyEntry = Entry | Question
type EntryListAttr = Literal["requirements", "constraints", "questions", "decisions"]

ENTRY_LIST_ATTRS: dict[Kind, EntryListAttr] = {
    "requirement": "requirements",
    "constraint": "constraints",
    "question": "questions",
    "decision": "decisions",
}


class ProjectBrief(BaseModel):
    """Top-level project framing stored in YAML."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    tldr: str | None = None
    goal: str | None = None
    target_user: str | None = None
    success_outcome: str | None = None
    software_type: str | None = None
    codebase_status: str | None = None


class Entry(BaseModel):
    """Durable requirement, constraint, or decision."""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    status: ActiveStatus = "active"
    archive_reason: str | None = None

    @model_validator(mode="after")
    def _validate_archive_reason(self) -> Self:
        if self.status == "archived" and not self.archive_reason:
            raise ValueError(f"Archived entry `{self.id}` must store archive_reason.")
        if self.status != "archived" and self.archive_reason is not None:
            raise ValueError(f"Active entry `{self.id}` must not store archive_reason.")
        return self


class Question(BaseModel):
    """Question that can stay open or link to a decision."""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    status: QuestionStatus = "open"
    decision: str | None = None
    archive_reason: str | None = None

    @model_validator(mode="after")
    def _validate_status_fields(self) -> Self:
        if self.status == "open" and self.decision is not None:
            raise ValueError(f"Open question `{self.id}` must not link to a decision.")
        if self.status == "resolved" and not self.decision:
            raise ValueError(f"Resolved question `{self.id}` must link to a decision.")
        if self.status == "archived" and not self.archive_reason:
            raise ValueError(f"Archived question `{self.id}` must store archive_reason.")
        if self.status != "archived" and self.archive_reason is not None:
            raise ValueError(f"Question `{self.id}` must not store archive_reason unless archived.")
        return self


class _EntryGroup:
    def entries(self, kind: Kind) -> list[AnyEntry]:
        """Return the mutable list for one entry kind.

        Returns:
            Mutable entries for the requested kind.
        """
        return cast("list[AnyEntry]", getattr(self, ENTRY_LIST_ATTRS[kind]))

    def iter_entries(self) -> Iterator[tuple[Kind, AnyEntry]]:
        """Yield entries with their semantic kind.

        Yields:
            Entry kind and entry pairs.
        """
        for kind in ENTRY_LIST_ATTRS:
            for entry in self.entries(kind):
                yield kind, entry


class Section(_EntryGroup, BaseModel):
    """Reusable group of project-scoped entries."""

    model_config = ConfigDict(extra="forbid")

    requirements: list[Entry] = Field(default_factory=list)
    constraints: list[Entry] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    decisions: list[Entry] = Field(default_factory=list)


class Feature(_EntryGroup, BaseModel):
    """Feature-local brief plus scoped entries."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    summary: str
    requirements: list[Entry] = Field(default_factory=list)
    constraints: list[Entry] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    decisions: list[Entry] = Field(default_factory=list)


class Document(BaseModel):
    """Complete YAML document."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project: ProjectBrief = Field(default_factory=ProjectBrief)
    global_section: Section = Field(default_factory=Section, alias="global")
    features: list[Feature] = Field(default_factory=list)

    def find_feature(self, feature_id: str) -> Feature:
        """Return the feature matching an ID.

        Returns:
            The matching feature.

        Raises:
            ValueError: If the feature ID is unknown.
        """
        for feature in self.features:
            if feature.id == feature_id:
                return feature
        raise ValueError(f"Unknown feature ID `{feature_id}`.")

    def find_entry(self, entry_id: str) -> "EntryRef":
        """Return a located entry by ID.

        Returns:
            Located entry and its feature scope.

        Raises:
            ValueError: If the entry ID is unknown.
        """
        for kind, entry in self.global_section.iter_entries():
            if entry.id == entry_id:
                return EntryRef(kind, entry, None)
        for feature in self.features:
            for kind, entry in feature.iter_entries():
                if entry.id == entry_id:
                    return EntryRef(kind, entry, feature)
        raise ValueError(f"Unknown entry ID `{entry_id}`.")

    def find_question(self, question_id: str) -> "EntryRef":
        """Return a located question by ID.

        Returns:
            Located question entry and its feature scope.

        Raises:
            ValueError: If the ID is unknown or is not a question.
        """
        ref = self.find_entry(question_id)
        if ref.kind != "question":
            raise ValueError(f"`{question_id}` is not a question ID.")
        return ref

    def find_decision(self, decision_id: str) -> "EntryRef":
        """Return a located decision by ID.

        Returns:
            Located decision entry and its feature scope.

        Raises:
            ValueError: If the ID is unknown or is not a decision.
        """
        ref = self.find_entry(decision_id)
        if ref.kind != "decision":
            raise ValueError(f"`{decision_id}` is not a decision ID.")
        return ref

    def ensure_valid(self) -> None:
        """Raise if document entries or references are malformed.

        Raises:
            RuntimeError: If the document is internally inconsistent.
        """
        seen_feature_ids: set[str] = set()
        for feature in self.features:
            if not is_feature_id(feature.id):
                raise RuntimeError(f"Malformed notes.yaml: invalid feature ID `{feature.id}`.")
            if feature.id in seen_feature_ids:
                raise RuntimeError(f"Malformed notes.yaml: duplicate feature ID `{feature.id}`.")
            seen_feature_ids.add(feature.id)

        self._ensure_section(self.global_section, None)
        for feature in self.features:
            self._ensure_section(feature, feature.id)

    def ensure_state(self, state: "RuntimeState") -> None:
        """Raise if runtime focus points at invalid entries."""
        if state.focus.feature_id is not None:
            self.find_feature(state.focus.feature_id)
        for carry_id in state.focus.carry_ids:
            self.ensure_carry(carry_id)

    def ensure_carry(self, carry_id: str) -> None:
        """Raise if a carried focus ID is unknown or archived.

        Raises:
            ValueError: If a carried entry is archived or unknown.
        """
        if is_feature_id(carry_id):
            self.find_feature(carry_id)
            return

        ref = self.find_entry(carry_id)
        if ref.entry.status == "archived":
            raise ValueError(f"Archived entry `{carry_id}` cannot be carried into focus.")

    @staticmethod
    def _ensure_section(section: AnySection, feature_id: str | None) -> None:
        seen_ids: set[str] = set()
        decisions = {decision.id for decision in section.decisions}

        for kind, entry in section.iter_entries():
            if entry.id in seen_ids:
                raise RuntimeError(f"Malformed notes.yaml: duplicate entry ID `{entry.id}`.")
            seen_ids.add(entry.id)
            validate_entry_id(kind, entry.id, feature_id)
            if kind != "question":
                continue

            question = cast("Question", entry)
            if question.status != "resolved":
                continue
            if question.decision not in decisions:
                raise RuntimeError(
                    f"Malformed notes.yaml: resolved question `{question.id}` links to missing "
                    f"decision `{question.decision}`."
                )
            validate_entry_id("decision", cast("str", question.decision), feature_id)


class FocusState(BaseModel):
    """Current interview focus and carried context IDs."""

    model_config = ConfigDict(extra="forbid")

    scope: FocusScope = "project"
    feature_id: str | None = None
    carry_ids: list[str] = Field(default_factory=list)
    reason: str = "Initial project discovery."

    @model_validator(mode="after")
    def _validate_feature_scope(self) -> Self:
        if self.scope == "feature" and not self.feature_id:
            raise ValueError("Feature focus requires feature_id.")
        if self.scope != "feature" and self.feature_id is not None:
            raise ValueError("Only feature focus may store feature_id.")
        return self


class InterviewItem(BaseModel):
    """Visible interview transcript item."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["user", "assistant", "tool"]
    text: str


class InterviewState(BaseModel):
    """Runtime-only interview transcript and active model context."""

    model_config = ConfigDict(extra="forbid")

    items: list[InterviewItem] = Field(default_factory=list)
    context: list[dict[str, Any]] = Field(default_factory=list)


class RuntimeState(BaseModel):
    """Runtime-only state persisted outside YAML."""

    model_config = ConfigDict(extra="forbid")

    focus: FocusState = Field(default_factory=FocusState)
    interview: InterviewState = Field(default_factory=InterviewState)


class EntryRef(NamedTuple):
    """Located entry and the feature scope it belongs to."""

    kind: Kind
    entry: AnyEntry
    feature: Feature | None
