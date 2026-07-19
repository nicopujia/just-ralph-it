import logging
from difflib import SequenceMatcher
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, Self

from pydantic import BaseModel, Field, ValidationError, model_validator

from .exceptions import PersistenceError

logger = logging.getLogger(__name__)


class Note(BaseModel):
    """A single independently meaningful idea."""

    id: str
    text: str


class Connection(BaseModel):
    """A directed, labeled relationship between two notes."""

    source_id: str
    target_id: str
    label: str


class Graph(BaseModel):
    """The persisted note graph."""

    notes: list[Note] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_graph(self) -> "Graph":
        ids = [note.id for note in self.notes]
        if len(ids) != len(set(ids)):
            raise ValueError("Note IDs must be unique.")
        if any(not note.text.strip() for note in self.notes):
            raise ValueError("Note text cannot be blank.")
        triples = [(item.source_id, item.target_id, item.label) for item in self.connections]
        if len(triples) != len(set(triples)):
            raise ValueError("Connections must be unique.")
        for connection in self.connections:
            if not connection.label.strip():
                raise ValueError("Connection labels cannot be blank.")
            if connection.source_id not in ids or connection.target_id not in ids:
                raise ValueError("Connection endpoints must reference existing notes.")
        return self


class ReadQuery(BaseModel):
    """Select notes by text, ID, or graph traversal."""

    text: str | None = None
    ids: list[str] | None = None
    traverse_from: list[str] | None = None
    direction: Literal["outgoing", "incoming", "both"] | None = None
    depth: int | None = None

    @model_validator(mode="after")
    def validate_query(self) -> Self:
        if self.depth is not None and self.depth < 1:
            raise ValueError("Traversal depth must be at least 1.")
        if self.text is not None and not self.text.strip():
            raise ValueError("Search query cannot be blank.")
        return self


class Notebook:
    """Query and mutate the persisted interviewer note graph."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.graph = self._load()
        logger.info("initialized notes=%d connections=%d", len(self.graph.notes), len(self.graph.connections))

    def read(self, query: ReadQuery) -> tuple[list[Note], list[Connection]]:
        """Read notes by fuzzy text, ID, or graph traversal.

        Returns:
            Matching notes and the connections between them.

        Raises:
            ValueError: If selectors or traversal arguments are invalid.
        """
        by_id = {note.id: note for note in self.graph.notes}
        selected = dict(by_id) if query.text is None and not query.ids and not query.traverse_from else {}
        for note_id in query.ids or []:
            if note_id not in by_id:
                raise ValueError(f"Unknown note `{note_id}`.")
            selected[note_id] = by_id[note_id]

        if query.text is not None:
            normalized_query = query.text.casefold().strip()
            ranked = sorted(
                self.graph.notes,
                key=lambda note: (
                    normalized_query in note.text.casefold(),
                    SequenceMatcher(None, normalized_query, note.text.casefold()).ratio(),
                ),
                reverse=True,
            )
            for note in ranked[:10]:
                selected[note.id] = note

        frontier = set(query.traverse_from or [])
        unknown = frontier - by_id.keys()
        if unknown:
            raise ValueError(f"Unknown note `{min(unknown)}`.")
        selected.update((note_id, by_id[note_id]) for note_id in frontier)
        visited = set(frontier)
        traversal_direction = query.direction or "both"
        for _ in range(query.depth or 1):
            next_frontier: set[str] = set()
            for connection in self.graph.connections:
                if traversal_direction in {"outgoing", "both"} and connection.source_id in frontier:
                    next_frontier.add(connection.target_id)
                if traversal_direction in {"incoming", "both"} and connection.target_id in frontier:
                    next_frontier.add(connection.source_id)
            next_frontier -= visited
            selected.update((note_id, by_id[note_id]) for note_id in next_frontier)
            visited |= next_frontier
            frontier = next_frontier

        selected_ids = selected.keys()
        connections = [
            item for item in self.graph.connections if item.source_id in selected_ids and item.target_id in selected_ids
        ]
        notes = list(selected.values())
        logger.info("read_finished notes=%d connections=%d", len(notes), len(connections))
        return notes, connections

    def add(self, texts: list[str]) -> list[Note]:
        """Add multiple notes atomically.

        Returns:
            The added notes.

        Raises:
            ValueError: If the batch is empty or contains blank text.
        """
        if not texts or any(not text.strip() for text in texts):
            raise ValueError("Provide one or more non-blank note texts.")
        graph = self.graph.model_copy(deep=True)
        numbers = [int(note.id[1:]) for note in graph.notes if note.id.startswith("n") and note.id[1:].isdigit()]
        next_number = max(numbers, default=0) + 1
        added = [Note(id=f"n{next_number + index}", text=text) for index, text in enumerate(texts)]
        graph.notes.extend(added)
        self._save(graph)
        logger.info("add_finished ids=%r", [note.id for note in added])
        return added

    def edit(self, note_id: str, text: str) -> Note:
        """Edit a note without changing its connections.

        Returns:
            The edited note.

        Raises:
            ValueError: If the note is unknown or its text is blank.
        """
        if not text.strip():
            raise ValueError("Note text cannot be blank.")
        graph = self.graph.model_copy(deep=True)
        note = self._find(graph, note_id)
        note.text = text
        self._save(graph)
        logger.info("edit_finished note_id=%s", note.id)
        return note

    def delete(self, note_ids: list[str]) -> list[str]:
        """Delete notes and their connections atomically.

        Returns:
            The deleted note IDs.

        Raises:
            ValueError: If the batch or any note ID is invalid.
        """
        if not note_ids or len(note_ids) != len(set(note_ids)):
            raise ValueError("Provide one or more unique note IDs.")
        graph = self.graph.model_copy(deep=True)
        for note_id in note_ids:
            self._find(graph, note_id)
        deleted = set(note_ids)
        graph.notes = [note for note in graph.notes if note.id not in deleted]
        graph.connections = [
            item for item in graph.connections if item.source_id not in deleted and item.target_id not in deleted
        ]
        self._save(graph)
        logger.info("delete_finished note_ids=%r", note_ids)
        return note_ids

    def connect(self, connections: list[Connection]) -> int:
        """Connect notes atomically.

        Existing connections are no-ops.

        Returns:
            The number of relationships created.

        Raises:
            ValueError: If any requested connection is invalid.
        """
        if not connections:
            raise ValueError("Provide one or more connections.")
        graph = self.graph.model_copy(deep=True)
        existing = {(item.source_id, item.target_id, item.label) for item in graph.connections}
        requested = [(item.source_id, item.target_id, item.label) for item in connections]
        if len(requested) != len(set(requested)):
            raise ValueError("Connections in a request must be unique.")
        for connection in connections:
            self._find(graph, connection.source_id)
            self._find(graph, connection.target_id)
            if not connection.label.strip():
                raise ValueError("Connection labels cannot be blank.")
            if tuple(connection.model_dump().values()) not in existing:
                graph.connections.append(connection)
        self._save(graph)
        count = len(set(requested) - existing)
        logger.info("connect_finished count=%d", count)
        return count

    def disconnect(self, connections: list[Connection]) -> int:
        """Disconnect notes atomically.

        Missing connections are no-ops.

        Returns:
            The number of relationships removed.

        Raises:
            ValueError: If the requested batch is invalid.
        """
        if not connections:
            raise ValueError("Provide one or more connections.")
        graph = self.graph.model_copy(deep=True)
        requested = {(item.source_id, item.target_id, item.label) for item in connections}
        if len(requested) != len(connections):
            raise ValueError("Connections in a request must be unique.")
        for connection in connections:
            self._find(graph, connection.source_id)
            self._find(graph, connection.target_id)
            if not connection.label.strip():
                raise ValueError("Connection labels cannot be blank.")
        before = len(graph.connections)
        graph.connections = [
            item for item in graph.connections if (item.source_id, item.target_id, item.label) not in requested
        ]
        self._save(graph)
        count = before - len(graph.connections)
        logger.info("disconnect_finished count=%d", count)
        return count

    def restore(self, graph: Graph) -> None:
        """Restore a previous graph snapshot."""

        self._save(graph)

    def _load(self) -> Graph:
        if not self.path.exists():
            graph = Graph()
            self._write(graph)
            logger.debug("file_created")
            return graph
        try:
            graph = Graph.model_validate_json(self.path.read_text(encoding="utf-8"))
            logger.debug("file_loaded notes=%d connections=%d", len(graph.notes), len(graph.connections))
        except (OSError, ValidationError) as error:
            logger.exception("file_load_failed path=%r", self.path)
            raise PersistenceError(f"Invalid graph file `{self.path}`. Run JRI with --force to reset it.") from error
        else:
            return graph

    def _save(self, graph: Graph) -> None:
        graph = Graph.model_validate(graph)
        self._write(graph)
        self.graph = graph
        logger.debug("saved notes=%d connections=%d", len(graph.notes), len(graph.connections))

    def _write(self, graph: Graph) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", dir=self.path.parent, delete=False, encoding="utf-8") as file:
            file.write(f"{graph.model_dump_json(indent=2)}\n")
            temporary_path = file.name
        try:
            Path(temporary_path).replace(self.path)
        except OSError:
            Path(temporary_path).unlink(missing_ok=True)
            raise

    @staticmethod
    def _find(graph: Graph, note_id: str) -> Note:
        for note in graph.notes:
            if note.id == note_id:
                return note
        raise ValueError(f"Unknown note `{note_id}`.")
