from typing import Any, Literal, NamedTuple, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ActiveStatus = Literal["active", "archived"]
QuestionStatus = Literal["open", "resolved", "archived"]
NoteKind = Literal["requirement", "constraint", "question", "decision"]
ReadScope = Literal["all", "project", "global", "feature"]
ReadKind = Literal["all", "brief", "requirements", "constraints", "questions", "decisions", "features"]
FocusScope = Literal["project", "global", "feature"]
type NoteListAttr = Literal["requirements", "constraints", "questions", "decisions"]


class ProjectBrief(BaseModel):
    """Top-level project framing stored in notes YAML."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    tldr: str | None = None
    goal: str | None = None
    target_user: str | None = None
    success_outcome: str | None = None
    software_type: str | None = None
    codebase_status: str | None = None


class TrackedNote(BaseModel):
    """Durable requirement, constraint, or decision note."""

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
    """Question note that can stay open or link to a decision."""

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
    """Reusable group of project or feature notes."""

    model_config = ConfigDict(extra="forbid")

    requirements: list[TrackedNote] = Field(default_factory=list)
    constraints: list[TrackedNote] = Field(default_factory=list)
    questions: list[QuestionNote] = Field(default_factory=list)
    decisions: list[TrackedNote] = Field(default_factory=list)


class FeatureNotes(BaseModel):
    """Feature-local brief plus its scoped notes."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    summary: str
    requirements: list[TrackedNote] = Field(default_factory=list)
    constraints: list[TrackedNote] = Field(default_factory=list)
    questions: list[QuestionNote] = Field(default_factory=list)
    decisions: list[TrackedNote] = Field(default_factory=list)


class NotesDocument(BaseModel):
    """Complete notes YAML document."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    project: ProjectBrief = Field(default_factory=ProjectBrief)
    global_notes: NotesSection = Field(default_factory=NotesSection, alias="global")
    features: list[FeatureNotes] = Field(default_factory=list)


class FocusState(BaseModel):
    """Current interview focus and carried context IDs."""

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
    """Runtime-only state persisted outside notes YAML."""

    model_config = ConfigDict(extra="forbid")

    focus: FocusState = Field(default_factory=FocusState)
    interview: InterviewState = Field(default_factory=InterviewState)


type AnyNote = TrackedNote | QuestionNote
type AnySection = NotesSection | FeatureNotes


class NoteRef(NamedTuple):
    """Located note and the feature scope it belongs to."""

    kind: NoteKind
    note: AnyNote
    feature: FeatureNotes | None


class QuestionRef(NamedTuple):
    """Typed question lookup result."""

    note: QuestionNote
    feature: FeatureNotes | None


class DecisionRef(NamedTuple):
    """Typed decision lookup result."""

    note: TrackedNote
    feature: FeatureNotes | None
