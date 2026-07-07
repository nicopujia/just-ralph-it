from typing import cast

from .finders import find_note, get_feature, iter_section_notes
from .ids import is_feature_id, validate_note_id
from .models import AnySection, NotesDocument, QuestionNote, RuntimeState


def validate_document(document: NotesDocument) -> None:
    seen_feature_ids: set[str] = set()
    for feature in document.features:
        if not is_feature_id(feature.id):
            raise RuntimeError(f"Malformed notes.yaml: invalid feature ID `{feature.id}`.")
        if feature.id in seen_feature_ids:
            raise RuntimeError(f"Malformed notes.yaml: duplicate feature ID `{feature.id}`.")
        seen_feature_ids.add(feature.id)

    validate_section(document.global_notes, None)
    for feature in document.features:
        validate_section(feature, feature.id)


def validate_section(section: AnySection, feature_id: str | None) -> None:
    seen_ids: set[str] = set()
    decisions = {decision.id for decision in section.decisions}

    for kind, note in iter_section_notes(section):
        if note.id in seen_ids:
            raise RuntimeError(f"Malformed notes.yaml: duplicate note ID `{note.id}`.")
        seen_ids.add(note.id)
        validate_note_id(kind, note.id, feature_id)
        if kind != "question":
            continue

        question = cast("QuestionNote", note)
        if question.status != "resolved":
            continue
        if question.decision not in decisions:
            raise RuntimeError(
                f"Malformed notes.yaml: resolved question `{question.id}` links to missing "
                f"decision `{question.decision}`."
            )
        validate_note_id("decision", cast("str", question.decision), feature_id)


def validate_state(document: NotesDocument, state: RuntimeState) -> None:
    if state.focus.feature_id is not None:
        get_feature(document, state.focus.feature_id)
    for carry_id in state.focus.carry_ids:
        validate_carry_id(document, carry_id)


def validate_carry_id(document: NotesDocument, carry_id: str) -> None:
    if is_feature_id(carry_id):
        get_feature(document, carry_id)
        return

    ref = find_note(document, carry_id)
    if ref.note.status == "archived":
        raise ValueError(f"Archived note `{carry_id}` cannot be carried into focus.")
