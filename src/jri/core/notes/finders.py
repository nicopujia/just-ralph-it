from collections.abc import Iterator
from typing import cast

from .models import (
    AnyNote,
    AnySection,
    DecisionRef,
    FeatureNotes,
    NoteKind,
    NoteListAttr,
    NoteRef,
    NotesDocument,
    QuestionNote,
    QuestionRef,
    TrackedNote,
)

NOTE_LIST_ATTRS: dict[NoteKind, NoteListAttr] = {
    "requirement": "requirements",
    "constraint": "constraints",
    "question": "questions",
    "decision": "decisions",
}


def iter_section_notes(section: AnySection) -> Iterator[tuple[NoteKind, AnyNote]]:
    for kind in NOTE_LIST_ATTRS:
        for note in list_notes(section, kind):
            yield kind, note


def list_notes(section: AnySection, kind: NoteKind) -> list[AnyNote]:
    return cast("list[AnyNote]", getattr(section, NOTE_LIST_ATTRS[kind]))


def get_feature(document: NotesDocument, feature_id: str) -> FeatureNotes:
    for feature in document.features:
        if feature.id == feature_id:
            return feature
    raise ValueError(f"Unknown feature ID `{feature_id}`.")


def find_note(document: NotesDocument, note_id: str) -> NoteRef:
    for kind, note in iter_section_notes(document.global_notes):
        if note.id == note_id:
            return NoteRef(kind, note, None)
    for feature in document.features:
        for kind, note in iter_section_notes(feature):
            if note.id == note_id:
                return NoteRef(kind, note, feature)
    raise ValueError(f"Unknown note ID `{note_id}`.")


def find_question(document: NotesDocument, question_id: str) -> QuestionRef:
    ref = find_note(document, question_id)
    if ref.kind != "question":
        raise ValueError(f"`{question_id}` is not a question ID.")
    return QuestionRef(cast("QuestionNote", ref.note), ref.feature)


def find_decision(document: NotesDocument, decision_id: str) -> DecisionRef:
    ref = find_note(document, decision_id)
    if ref.kind != "decision":
        raise ValueError(f"`{decision_id}` is not a decision ID.")
    return DecisionRef(cast("TrackedNote", ref.note), ref.feature)
