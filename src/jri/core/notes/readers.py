from typing import Literal, cast

from .finders import find_note, get_feature, list_notes
from .ids import is_feature_id
from .models import AnyNote, AnySection, NoteKind, NotesDocument, QuestionNote, ReadKind, ReadScope

READ_NOTE_KINDS: dict[ReadKind, NoteKind] = {
    "requirements": "requirement",
    "constraints": "constraint",
    "questions": "question",
    "decisions": "decision",
}
USER_CONTROL_PREFERENCE_MARKER = "user control preference"


def read_notes(
    document: NotesDocument,
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
        return render_ids(document, ids, include_archived=include)

    selected_scope = scope or "all"
    if feature_id is not None and selected_scope != "feature":
        raise ValueError("feature_id is only valid when scope is `feature`.")

    lines: list[str] = []
    if selected_scope == "all":
        render_all_scope(document, lines, selected_kind, include_archived=include)
    elif selected_scope == "project":
        if selected_kind in {"all", "brief"}:
            render_project_brief(document, lines)
    elif selected_scope == "global":
        render_section(lines, "Global", document.global_notes, selected_kind, include_archived=include)
    else:
        render_feature_scope(document, lines, selected_kind, feature_id, include_archived=include)

    return "\n".join(lines).strip() or "No notes found."


def get_user_control_preference_state(document: NotesDocument) -> Literal["absent", "open", "resolved"]:
    if any(
        decision.status == "active" and USER_CONTROL_PREFERENCE_MARKER in decision.text.casefold()
        for decision in document.global_notes.decisions
    ):
        return "resolved"

    active_global_decision_ids = {
        decision.id for decision in document.global_notes.decisions if decision.status == "active"
    }
    if any(
        question.status == "resolved"
        and question.decision in active_global_decision_ids
        and USER_CONTROL_PREFERENCE_MARKER in question.text.casefold()
        for question in document.global_notes.questions
    ):
        return "resolved"
    if any(
        question.status == "open" and USER_CONTROL_PREFERENCE_MARKER in question.text.casefold()
        for question in document.global_notes.questions
    ):
        return "open"

    return "absent"


def render_all_scope(document: NotesDocument, lines: list[str], kind: ReadKind, *, include_archived: bool) -> None:
    if kind in {"all", "brief"}:
        render_project_brief(document, lines)
    if kind in {"all", "requirements", "constraints", "questions", "decisions"}:
        render_section(lines, "Global", document.global_notes, kind, include_archived=include_archived)
    if kind in {"all", "features"} and document.features:
        if lines:
            lines.append("")
        lines.append("Features")
        lines.extend(f"- {feature.id}: {feature.name} - {feature.summary}" for feature in document.features)


def render_feature_scope(
    document: NotesDocument, lines: list[str], kind: ReadKind, feature_id: str | None, *, include_archived: bool
) -> None:
    if feature_id is None:
        raise ValueError("feature_id is required when scope is `feature`.")

    feature = get_feature(document, feature_id)
    if kind in {"all", "brief"}:
        lines.append(f"Feature {feature.id}: {feature.name}. {feature.summary}")
    render_section(lines, f"Feature {feature.id}", feature, kind, include_archived=include_archived)


def render_ids(document: NotesDocument, ids: list[str], *, include_archived: bool) -> str:
    lines: list[str] = []
    for note_id in ids:
        if is_feature_id(note_id):
            feature = get_feature(document, note_id)
            lines.append(f"Feature {feature.id}: {feature.name}. {feature.summary}")
            continue

        ref = find_note(document, note_id)
        if ref.note.status == "archived" and not include_archived:
            continue
        scope = "Global" if ref.feature is None else f"Feature {ref.feature.id}"
        lines.append(f"{scope} {render_note(ref.kind, ref.note)}")

    return "\n".join(lines).strip() or "No notes found."


def render_project_brief(document: NotesDocument, lines: list[str]) -> None:
    project = document.project
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


def render_section(
    lines: list[str], scope_label: str, section: AnySection, kind: ReadKind, *, include_archived: bool
) -> None:
    for read_kind, note_kind in READ_NOTE_KINDS.items():
        if kind not in {"all", read_kind}:
            continue
        render_note_group(
            lines,
            f"{scope_label} {read_kind}",
            note_kind,
            list_notes(section, note_kind),
            include_archived=include_archived,
        )


def render_note_group(
    lines: list[str], title: str, kind: NoteKind, section_notes: list[AnyNote], *, include_archived: bool
) -> None:
    visible_notes = [note for note in section_notes if include_archived or note.status != "archived"]
    if not visible_notes:
        return
    if lines:
        lines.append("")
    lines.append(title)
    lines.extend(f"- {render_note(kind, note)}" for note in visible_notes)


def render_note(kind: NoteKind, note: AnyNote) -> str:
    rendered = f"{note.id}: {note.text}"
    if kind == "question":
        question = cast("QuestionNote", note)
        rendered += f" ({question.status}"
        if question.decision:
            rendered += f", decision: {question.decision}"
        rendered += ")"
    elif note.status != "active":
        rendered += f" ({note.status})"
    if note.archive_reason:
        rendered += f" Reason: {note.archive_reason}"
    return rendered
