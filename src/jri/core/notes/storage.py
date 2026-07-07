import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import Document, FocusState, RuntimeState


class Storage:
    """Load and save the YAML document and runtime state."""

    def __init__(self, document_file: Path, state_file: Path) -> None:
        """Create parent directories for the managed files."""
        self.document_file = document_file
        self.state_file = state_file
        self.document_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def load_document(self) -> Document:
        """Load the structured YAML document.

        Returns:
            Loaded document, or a new one if the file is empty.

        Raises:
            RuntimeError: If the YAML cannot be parsed or validated.
            TypeError: If the YAML root is not a mapping.
        """
        if not self.document_file.exists() or not self.document_file.read_text(encoding="utf-8").strip():
            document = Document()
            self.save_document(document)
            return document

        try:
            data = yaml.safe_load(self.document_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise RuntimeError(f"Malformed notes.yaml: {error}") from error

        if not isinstance(data, dict):
            raise TypeError("Malformed notes.yaml: expected a YAML mapping.")

        try:
            document = Document.model_validate(data)
        except ValidationError as error:
            raise RuntimeError(f"Malformed notes.yaml: {error}") from error

        document.ensure_valid()
        return document

    def save_document(self, document: Document) -> None:
        """Validate and write the structured YAML document."""
        document.ensure_valid()
        data = document.model_dump(by_alias=True, mode="json", exclude_none=True)
        data["project"] = document.project.model_dump(mode="json")
        self.document_file.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")

    def load_state(self, document: Document) -> RuntimeState:
        """Load runtime state and repair stale focus IDs.

        Returns:
            The loaded runtime state.

        Raises:
            RuntimeError: If the JSON cannot be parsed or validated.
        """
        if not self.state_file.exists() or not self.state_file.read_text(encoding="utf-8").strip():
            state = RuntimeState()
            self.save_state(document, state)
            return state

        try:
            data: Any = json.loads(self.state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Malformed .jri/state.json: {error}") from error

        try:
            state = RuntimeState.model_validate(data)
        except ValidationError as error:
            raise RuntimeError(f"Malformed .jri/state.json: {error}") from error

        active_carry_ids: list[str] = []
        for carry_id in state.focus.carry_ids:
            try:
                document.ensure_carry(carry_id)
            except ValueError:
                continue
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
            self.save_state(document, state)

        document.ensure_state(state)
        return state

    def save_state(self, document: Document, state: RuntimeState) -> None:
        """Validate and write runtime state JSON."""
        document.ensure_state(state)
        self.state_file.write_text(f"{json.dumps(state.model_dump(mode='json'), indent=2)}\n", encoding="utf-8")
