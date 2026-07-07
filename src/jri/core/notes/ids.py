import re
from collections.abc import Iterable

from .models import NoteKind

FEATURE_ID_PATTERN = re.compile(r"f([1-9][0-9]*)")
GLOBAL_ID_PATTERNS = {
    "requirement": re.compile(r"r([1-9][0-9]*)"),
    "constraint": re.compile(r"c([1-9][0-9]*)"),
    "question": re.compile(r"q([1-9][0-9]*)"),
    "decision": re.compile(r"d([1-9][0-9]*)"),
}
NOTE_PREFIXES: dict[NoteKind, str] = {"requirement": "r", "constraint": "c", "question": "q", "decision": "d"}


def is_feature_id(note_id: str) -> bool:
    return FEATURE_ID_PATTERN.fullmatch(note_id) is not None


def allocate_feature_id(feature_ids: Iterable[str]) -> str:
    highest = 0
    for feature_id in feature_ids:
        if match := FEATURE_ID_PATTERN.fullmatch(feature_id):
            highest = max(highest, int(match.group(1)))
    return f"f{highest + 1}"


def allocate_note_id(kind: NoteKind, note_ids: Iterable[str], feature_id: str | None) -> str:
    prefix = NOTE_PREFIXES[kind]
    highest = 0
    for note_id in note_ids:
        local_id = note_id.removeprefix(f"{feature_id}/") if feature_id is not None else note_id
        if match := re.fullmatch(rf"{prefix}([1-9][0-9]*)", local_id):
            highest = max(highest, int(match.group(1)))
    note_id = f"{prefix}{highest + 1}"
    return f"{feature_id}/{note_id}" if feature_id is not None else note_id


def validate_note_id(kind: NoteKind, note_id: str, feature_id: str | None) -> None:
    local_id = note_id
    if feature_id is not None:
        prefix = f"{feature_id}/"
        if not note_id.startswith(prefix):
            raise RuntimeError(f"Malformed notes.yaml: `{note_id}` must be scoped under `{feature_id}`.")
        local_id = note_id.removeprefix(prefix)
    elif "/" in note_id:
        raise RuntimeError(f"Malformed notes.yaml: global note ID `{note_id}` must not contain `/`.")

    if not GLOBAL_ID_PATTERNS[kind].fullmatch(local_id):
        raise RuntimeError(f"Malformed notes.yaml: invalid {kind} ID `{note_id}`.")
