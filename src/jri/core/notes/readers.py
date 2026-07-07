from typing import cast

from .finders import find_note, get_feature, list_notes
from .ids import is_feature_id
from .models import AnyNote, AnySection, NoteKind, NotesDocument, QuestionNote, ReadKind, ReadScope

READ_NOTE_KINDS: dict[ReadKind, NoteKind] = {
    "requirements": "requirement",
    "constraints": "constraint",
    "questions": "question",
    "decisions": "decision",
}


def read_notes(
    document: NotesDocument,
    scope: ReadScope | None,
    kind: ReadKind | None,
    feature_id: str | None,
    ids: list[str] | None,
    *,
    include_archived: bool | None,
) -> str:
    """Render selected notes as compact agent-readable text.

    Returns:
        Rendered notes or the standard empty result.
    """
    reader = Reader(document, kind or "all", include_archived=include_archived is True)
    if ids:
        reader.render_ids(ids)
    else:
        reader.render(scope or "all", feature_id)
    return reader.get_text()


class Reader:
    """Accumulate rendered notes for one read request."""

    def __init__(self, document: NotesDocument, kind: ReadKind, *, include_archived: bool) -> None:
        """Store request-wide rendering state."""
        self.document = document
        self.kind = kind
        self.include_archived = include_archived
        self.lines: list[str] = []

    def render(self, scope: ReadScope, feature_id: str | None) -> None:
        """Render notes selected by scope and optional feature ID.

        Raises:
            ValueError: If feature scope inputs are invalid.
        """
        if feature_id is not None and scope != "feature":
            raise ValueError("feature_id is only valid when scope is `feature`.")

        if scope in {"all", "project"} and self.kind in {"all", "brief"}:
            self.render_project_brief()
        if scope in {"all", "global"}:
            self.render_section("Global", self.document.global_notes)
        elif scope != "project":
            if feature_id is None:
                raise ValueError("feature_id is required when scope is `feature`.")
            feature = get_feature(self.document, feature_id)
            if self.kind in {"all", "brief"}:
                self.lines.append(f"Feature {feature.id}: {feature.name}. {feature.summary}")
            self.render_section(f"Feature {feature.id}", feature)
        if scope == "all" and self.kind in {"all", "features"}:
            self.render_block(
                "Features",
                [f"- {feature.id}: {feature.name} - {feature.summary}" for feature in self.document.features],
            )

    def render_ids(self, ids: list[str]) -> None:
        """Render notes and features selected by explicit IDs."""
        for note_id in ids:
            if is_feature_id(note_id):
                feature = get_feature(self.document, note_id)
                self.lines.append(f"Feature {feature.id}: {feature.name}. {feature.summary}")
                continue

            ref = find_note(self.document, note_id)
            if ref.note.status == "archived" and not self.include_archived:
                continue
            scope_label = "Global" if ref.feature is None else f"Feature {ref.feature.id}"
            self.lines.append(f"{scope_label} {render_note(ref.kind, ref.note)}")

    def render_project_brief(self) -> None:
        """Render the project brief section."""
        project = self.document.project
        title = project.name or "Untitled project"
        self.lines.append(f"# {title}")
        if project.tldr:
            self.lines.append(f"TL;DR: {project.tldr}")

        for label, value in [
            ("Goal", project.goal),
            ("Target user", project.target_user),
            ("Success outcome", project.success_outcome),
            ("Software type", project.software_type),
            ("Codebase status", project.codebase_status),
        ]:
            if value:
                self.lines.append(f"- {label}: {value}")
        if len(self.lines) == 1 and title == "Untitled project":
            self.lines.append("Project brief: not set.")

    def render_section(self, scope_label: str, section: AnySection) -> None:
        """Render matching note groups from one note section."""
        for read_kind, note_kind in READ_NOTE_KINDS.items():
            if self.kind in {"all", read_kind}:
                self.render_block(
                    f"{scope_label} {read_kind}",
                    [
                        f"- {render_note(note_kind, note)}"
                        for note in list_notes(section, note_kind)
                        if self.include_archived or note.status != "archived"
                    ],
                )

    def render_block(self, title: str, block_lines: list[str]) -> None:
        """Append one titled block when it has visible lines."""
        if block_lines:
            self.lines.extend(("", title) if self.lines else (title,))
            self.lines.extend(block_lines)

    def get_text(self) -> str:
        """Return the rendered text or the standard empty result.

        Returns:
            Rendered notes or the standard empty result.
        """
        return "\n".join(self.lines).strip() or "No notes found."


def render_note(kind: NoteKind, note: AnyNote) -> str:
    """Render one note line.

    Returns:
        A compact note line with status metadata.
    """
    rendered = f"{note.id}: {note.text}"
    if kind == "question":
        question = cast("QuestionNote", note)
        decision = f", decision: {question.decision}" if question.decision else ""
        rendered += f" ({question.status}{decision})"
    elif note.status != "active":
        rendered += f" ({note.status})"
    if note.archive_reason:
        rendered += f" Reason: {note.archive_reason}"
    return rendered
