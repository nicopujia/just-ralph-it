import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .finders import find_note
from .ids import is_feature_id
from .models import FocusState, NotesDocument, RuntimeState
from .validation import validate_document, validate_state


def load_notes(notes_file: Path) -> NotesDocument:
    if not notes_file.exists() or not notes_file.read_text(encoding="utf-8").strip():
        document = NotesDocument()
        save_notes(notes_file, document)
        return document

    try:
        data = yaml.safe_load(notes_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise RuntimeError(f"Malformed notes.yaml: {error}") from error

    if not isinstance(data, dict):
        raise TypeError("Malformed notes.yaml: expected a YAML mapping.")

    try:
        document = NotesDocument.model_validate(data)
    except ValidationError as error:
        raise RuntimeError(f"Malformed notes.yaml: {error}") from error

    validate_document(document)
    return document


def save_notes(notes_file: Path, document: NotesDocument) -> None:
    validate_document(document)
    data = document.model_dump(by_alias=True, mode="json", exclude_none=True)
    data["project"] = document.project.model_dump(mode="json")
    notes_file.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def load_state(state_file: Path, document: NotesDocument) -> RuntimeState:
    if not state_file.exists() or not state_file.read_text(encoding="utf-8").strip():
        state = RuntimeState()
        save_state(state_file, document, state)
        return state

    try:
        data: Any = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Malformed .jri/state.json: {error}") from error

    try:
        state = RuntimeState.model_validate(data)
    except ValidationError as error:
        raise RuntimeError(f"Malformed .jri/state.json: {error}") from error

    active_carry_ids: list[str] = []
    for carry_id in state.focus.carry_ids:
        if is_feature_id(carry_id):
            if any(feature.id == carry_id for feature in document.features):
                active_carry_ids.append(carry_id)
            continue
        try:
            ref = find_note(document, carry_id)
        except ValueError:
            continue
        if ref.note.status != "archived":
            active_carry_ids.append(carry_id)

    state_changed = False
    if active_carry_ids != state.focus.carry_ids:
        state.focus.carry_ids = active_carry_ids
        state_changed = True
    if state.focus.feature_id is not None and all(
        feature.id != state.focus.feature_id for feature in document.features
    ):
        state.focus = FocusState()
        state_changed = True
    if state_changed:
        save_state(state_file, document, state)

    validate_state(document, state)
    return state


def save_state(state_file: Path, document: NotesDocument, state: RuntimeState) -> None:
    validate_state(document, state)
    state_file.write_text(f"{json.dumps(state.model_dump(mode='json'), indent=2)}\n", encoding="utf-8")
