import logging
from difflib import SequenceMatcher
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, model_validator
from yaml import YAMLError, safe_dump, safe_load

from .exceptions import PersistenceError

type TopicId = Annotated[str, Field(pattern=r"^t\d+$")]
type NoteId = Annotated[str, Field(pattern=r"^n\d+$")]
type NodeId = Annotated[str, Field(pattern=r"^[nt]\d+$")]

logger = logging.getLogger(__name__)


class Topic(BaseModel):
    """A named area of the notebook."""

    id: TopicId
    name: str
    status: Literal["open", "done", "trashed"]
    summary: str | None = None


class Note(BaseModel):
    """A single independently meaningful idea."""

    id: NoteId
    topic_id: TopicId
    text: str


class Connection(BaseModel):
    """A directed, labeled relationship between two graph nodes."""

    source_id: NodeId
    target_id: NodeId
    label: str


class Graph(BaseModel):
    """The persisted note graph."""

    topics: list[Topic] = Field(default_factory=lambda: [Topic(id="t1", name="Project overview", status="open")])
    notes: list[Note] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_graph(self) -> "Graph":
        topic_ids = [topic.id for topic in self.topics]
        ids = topic_ids + [note.id for note in self.notes]
        if len(ids) != len(set(ids)):
            raise ValueError("Topic and note IDs must be unique.")
        content = [
            *ids,
            *(topic.name for topic in self.topics),
            *(topic.summary for topic in self.topics if topic.summary is not None),
            *(note.text for note in self.notes),
            *(connection.label for connection in self.connections),
        ]
        if any(not value.strip() for value in content):
            raise ValueError("Graph content cannot be blank.")
        if "t1" not in topic_ids:
            raise ValueError("The overview topic `t1` must exist.")
        names = [topic.name.strip().casefold() for topic in self.topics]
        if len(names) != len(set(names)):
            raise ValueError("Topic names must be unique.")
        if any(note.topic_id not in topic_ids for note in self.notes):
            raise ValueError("Every note must reference an existing topic.")
        triples = [(item.source_id, item.target_id, item.label) for item in self.connections]
        if len(triples) != len(set(triples)):
            raise ValueError("Connections must be unique.")
        for connection in self.connections:
            if connection.source_id not in ids or connection.target_id not in ids:
                raise ValueError("Connection endpoints must reference existing topics or notes.")
        return self


class ReadQuery(BaseModel):
    """Select notes by text, ID, or graph traversal."""

    text: str | None = None
    ids: list[NoteId] | None = None
    topic_ids: list[TopicId] | None = None
    traverse_from: list[NodeId] | None = None
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

    @property
    def initial_topic(self) -> Topic:
        """Return the overview topic every graph is validated to hold.

        Returns:
            The overview topic.
        """

        return next(topic for topic in self.graph.topics if topic.id == "t1")

    def read(self, query: ReadQuery) -> tuple[list[Note], list[Connection]]:
        """Read notes by fuzzy text, ID, or graph traversal.

        Returns:
            Matching notes and the connections between them.

        Raises:
            ValueError: If selectors or traversal arguments are invalid.
        """
        topic_ids = {topic.id for topic in self.graph.topics}
        if not set(query.topic_ids or []) <= topic_ids:
            raise ValueError(f"Unknown topic `{min(set(query.topic_ids or []) - topic_ids)}`.")
        allowed_topics = (
            set(query.topic_ids)
            if query.topic_ids is not None
            else {topic.id for topic in self.graph.topics if topic.status != "trashed"}
        )
        by_id = {note.id: note for note in self.graph.notes}
        candidates = {note.id: note for note in self.graph.notes if note.topic_id in allowed_topics}
        selected = dict(candidates) if query.text is None and not query.ids and not query.traverse_from else {}
        if not set(query.ids or []) <= by_id.keys():
            raise ValueError(f"Unknown note `{min(set(query.ids or []) - by_id.keys())}`.")
        selected.update((note_id, by_id[note_id]) for note_id in query.ids or [])

        if query.text is not None:
            normalized_query = query.text.casefold().strip()
            ranked = sorted(
                candidates.values(),
                key=lambda note: (
                    normalized_query in note.text.casefold(),
                    SequenceMatcher(None, normalized_query, note.text.casefold()).ratio(),
                ),
                reverse=True,
            )
            for note in ranked[:10]:
                selected[note.id] = note

        frontier = set(query.traverse_from or [])
        unknown = frontier - (by_id.keys() | topic_ids)
        if unknown:
            raise ValueError(f"Unknown topic or note `{min(unknown)}`.")
        selected.update((note_id, by_id[note_id]) for note_id in frontier & by_id.keys())
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
            selected.update((note_id, by_id[note_id]) for note_id in next_frontier & by_id.keys())
            visited |= next_frontier
            frontier = next_frontier

        selected = {note_id: note for note_id, note in selected.items() if note.topic_id in allowed_topics}
        selected_ids = set(selected)
        visible_ids = selected_ids | allowed_topics
        connections = [
            item
            for item in self.graph.connections
            if item.source_id in visible_ids
            and item.target_id in visible_ids
            and (
                item.source_id in selected_ids
                or item.target_id in selected_ids
                or (item.source_id in visited and item.target_id in visited)
                or (not query.traverse_from and query.text is None and not query.ids)
            )
        ]
        logger.info("read_finished notes=%d connections=%d", len(selected), len(connections))
        return list(selected.values()), connections

    def add(self, texts: list[str], topic_id: str) -> list[Note]:
        """Add multiple notes atomically.

        Returns:
            The added notes.

        Raises:
            ValueError: If the topic is unknown or the batch contains no
                usable text.
        """
        if not texts or any(not text.strip() for text in texts):
            raise ValueError("Provide one or more non-blank note texts.")
        graph = self.graph.model_copy(deep=True)
        self._find_topic(graph, topic_id)
        next_number = max((int(note.id[1:]) for note in graph.notes), default=0) + 1
        added = [Note(id=f"n{next_number + index}", topic_id=topic_id, text=text) for index, text in enumerate(texts)]
        graph.notes.extend(added)
        self._save(graph)
        logger.info("add_finished ids=%r", [note.id for note in added])
        return added

    def add_topic(self, name: str) -> Topic:
        """Add an open topic.

        Returns:
            The added topic.

        Raises:
            ValueError: If the name is blank or already used.
        """

        if not name.strip():
            raise ValueError("Topic name cannot be blank.")
        graph = self.graph.model_copy(deep=True)
        if any(topic.name.strip().casefold() == name.strip().casefold() for topic in graph.topics):
            raise ValueError(f"Topic `{name.strip()}` already exists.")
        next_number = max((int(topic.id[1:]) for topic in graph.topics), default=0) + 1
        topic = Topic(id=f"t{next_number}", name=name.strip(), status="open")
        graph.topics.append(topic)
        self._save(graph)
        logger.info("add_topic_finished topic_id=%s", topic.id)
        return topic

    def find_topic(self, value: str) -> Topic | None:
        """Find a topic by ID or case-insensitive name.

        Returns:
            The matching topic, if one exists.
        """

        for topic in self.graph.topics:
            if topic.id == value or topic.name.strip().casefold() == value.strip().casefold():
                return topic
        return None

    def update_topic(
        self, topic_id: str, status: Literal["open", "done", "trashed"], summary: str | None = None
    ) -> Topic:
        """Update a topic's status and optionally its summary.

        Returns:
            The updated topic.

        Raises:
            ValueError: If the topic is unknown or the update is
                invalid.
        """

        graph = self.graph.model_copy(deep=True)
        if summary is not None and not summary.strip():
            raise ValueError("Topic summary cannot be blank.")
        topic = self._find_topic(graph, topic_id)
        topic.status = status
        if summary is not None:
            topic.summary = summary
        self._save(graph)
        logger.info("update_topic_finished topic_id=%s status=%s", topic.id, topic.status)
        return topic

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
        note = self._find_note(graph, note_id)
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
            self._find_note(graph, note_id)
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
            self._find_node(graph, connection.source_id)
            self._find_node(graph, connection.target_id)
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
            self._find_node(graph, connection.source_id)
            self._find_node(graph, connection.target_id)
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

    def render(self, topic_id: TopicId) -> str:
        """Render topic-aware project YAML for model context.

        Returns:
            Visible topics and relevant notes.
        """

        return self._dump(self.graph, topic_id)

    def _load(self) -> Graph:
        if not self.path.exists():
            graph = Graph()
            self._write(graph)
            logger.debug("file_created")
            return graph
        try:
            graph = self._parse(safe_load(self.path.read_text(encoding="utf-8")))
            logger.debug("file_loaded notes=%d connections=%d", len(graph.notes), len(graph.connections))
        except (AttributeError, KeyError, OSError, TypeError, ValueError, YAMLError) as error:
            logger.exception("file_load_failed path=%r", self.path)
            raise PersistenceError(
                f"Invalid notebook file `{self.path}`. Run `jri init --force` to reset it."
            ) from error
        else:
            return graph

    @staticmethod
    def _parse(data: dict[str, Any]) -> Graph:
        topics = data["topics"]
        notes = [
            {"id": note_id, "topic_id": topic["id"], "text": text}
            for topic in topics
            for note_id, text in topic.pop("notes").items()
        ]
        notes.sort(key=lambda note: int(note["id"][1:]))
        connections = []
        for value in data["connections"]:
            source_id, label_and_target = value.split(" ", maxsplit=1)
            label, target_id = label_and_target.rsplit(" ", maxsplit=1)
            connections.append({"source_id": source_id, "target_id": target_id, "label": label})
        return Graph.model_validate({"topics": topics, "notes": notes, "connections": connections})

    def _save(self, graph: Graph) -> None:
        graph = Graph.model_validate(graph)
        self._write(graph)
        self.graph = graph
        logger.debug("saved notes=%d connections=%d", len(graph.notes), len(graph.connections))

    def _write(self, graph: Graph) -> None:
        temporary_path: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile("w", dir=self.path.parent, delete=False, encoding="utf-8") as file:
                temporary_path = file.name
                file.write(self._dump(graph))
            Path(temporary_path).replace(self.path)
        except OSError as error:
            if temporary_path is not None:
                Path(temporary_path).unlink(missing_ok=True)
            logger.exception("file_write_failed path=%r", self.path)
            raise PersistenceError(f"Could not save the notebook file `{self.path}`: {error.strerror}") from error

    @staticmethod
    def _dump(graph: Graph, topic_id: TopicId | None = None) -> str:
        topics = []
        for topic in graph.topics:
            if topic_id is not None and topic.status == "trashed":
                continue
            data = topic.model_dump(exclude_none=True)
            if topic_id is None or topic.id in {"t1", topic_id}:
                data["notes"] = {note.id: note.text for note in graph.notes if note.topic_id == topic.id}
            topics.append(data)
        data: dict[str, object] = {"topics": topics}
        if topic_id is None:
            data["connections"] = [
                f"{connection.source_id} {connection.label} {connection.target_id}" for connection in graph.connections
            ]
        return safe_dump(data, sort_keys=False, allow_unicode=True, width=10**9)

    @staticmethod
    def _find_note(graph: Graph, note_id: str) -> Note:
        for note in graph.notes:
            if note.id == note_id:
                return note
        raise ValueError(f"Unknown note `{note_id}`.")

    @staticmethod
    def _find_topic(graph: Graph, topic_id: str) -> Topic:
        for topic in graph.topics:
            if topic.id == topic_id:
                return topic
        raise ValueError(f"Unknown topic `{topic_id}`.")

    @staticmethod
    def _find_node(graph: Graph, node_id: str) -> Topic | Note:
        for node in [*graph.topics, *graph.notes]:
            if node.id == node_id:
                return node
        raise ValueError(f"Unknown topic or note `{node_id}`.")
