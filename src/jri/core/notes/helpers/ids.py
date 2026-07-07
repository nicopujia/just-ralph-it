import re
from collections.abc import Iterable
from typing import Literal

Kind = Literal["requirement", "constraint", "question", "decision"]

FEATURE_ID_PATTERN = re.compile(r"f([1-9][0-9]*)")
GLOBAL_ID_PATTERNS = {
    "requirement": re.compile(r"r([1-9][0-9]*)"),
    "constraint": re.compile(r"c([1-9][0-9]*)"),
    "question": re.compile(r"q([1-9][0-9]*)"),
    "decision": re.compile(r"d([1-9][0-9]*)"),
}
ENTRY_PREFIXES: dict[Kind, str] = {"requirement": "r", "constraint": "c", "question": "q", "decision": "d"}


def is_feature_id(entry_id: str) -> bool:
    """Return whether an ID points at a feature.

    Returns:
        Whether the ID has feature ID shape.
    """
    return FEATURE_ID_PATTERN.fullmatch(entry_id) is not None


def allocate_feature_id(feature_ids: Iterable[str]) -> str:
    """Return the next feature ID after the highest existing one.

    Returns:
        The next feature ID.
    """
    highest = 0
    for feature_id in feature_ids:
        if match := FEATURE_ID_PATTERN.fullmatch(feature_id):
            highest = max(highest, int(match.group(1)))
    return f"f{highest + 1}"


def allocate_entry_id(kind: Kind, entry_ids: Iterable[str], feature_id: str | None) -> str:
    """Return the next entry ID for a kind and feature scope.

    Returns:
        The next entry ID.
    """
    prefix = ENTRY_PREFIXES[kind]
    highest = 0
    for entry_id in entry_ids:
        local_id = entry_id.removeprefix(f"{feature_id}/") if feature_id is not None else entry_id
        if match := re.fullmatch(rf"{prefix}([1-9][0-9]*)", local_id):
            highest = max(highest, int(match.group(1)))
    entry_id = f"{prefix}{highest + 1}"
    return f"{feature_id}/{entry_id}" if feature_id is not None else entry_id


def validate_entry_id(kind: Kind, entry_id: str, feature_id: str | None) -> None:
    """Raise if an entry ID is invalid for its kind and scope.

    Raises:
        RuntimeError: If the ID is malformed.
    """
    local_id = entry_id
    if feature_id is not None:
        prefix = f"{feature_id}/"
        if not entry_id.startswith(prefix):
            raise RuntimeError(f"Malformed notes.yaml: `{entry_id}` must be scoped under `{feature_id}`.")
        local_id = entry_id.removeprefix(prefix)
    elif "/" in entry_id:
        raise RuntimeError(f"Malformed notes.yaml: global entry ID `{entry_id}` must not contain `/`.")

    if not GLOBAL_ID_PATTERNS[kind].fullmatch(local_id):
        raise RuntimeError(f"Malformed notes.yaml: invalid {kind} ID `{entry_id}`.")
