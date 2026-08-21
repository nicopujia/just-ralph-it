import logging
from difflib import SequenceMatcher
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, model_validator
from yaml import YAMLError, safe_dump, safe_load

from jri.lib import files

from .exceptions import PersistenceError

type TopicId = Annotated[str, Field(pattern=r"^t\d+$")]
type NoteId = Annotated[str, Field(pattern=r"^n\d+$")]

OVERVIEW_TOPIC_ID = "t1"
# The three levels are the root, a topic, and a subtopic.
# JRI pins every ancestor of the active topic on each request.
# One more level sends one more body with each request.
# A deeper tree also makes the correct topic for a note more difficult to find.
MAX_DEPTH = 3

logger = logging.getLogger(__name__)


class Topic(BaseModel):
    id: TopicId
    # This field holds the position of the topic. The file shows that position as nesting.
    parent_id: TopicId | None = None
    name: str
    summary: str | None = None
    status: Literal["open", "done", "trashed"]


class Note(BaseModel):
    id: NoteId
    topic_id: TopicId
    text: str


class Connection(BaseModel):
    source_id: NoteId
    target_id: NoteId
    label: str


class Graph(BaseModel):
    topics: list[Topic]
    notes: list[Note] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)
    next_note_id: NoteId = "n1"

    def read_overview(self) -> Topic:
        return next(topic for topic in self.topics if topic.id == OVERVIEW_TOPIC_ID)

    def read_subtopics(self) -> dict[str, list[Topic]]:
        subtopics: dict[str, list[Topic]] = {}
        for topic in self.topics:
            if topic.parent_id is not None:
                subtopics.setdefault(topic.parent_id, []).append(topic)
        return subtopics

    def read_ancestor_ids(self, topic_id: str) -> set[str]:
        by_id = {topic.id: topic for topic in self.topics}
        current = by_id[topic_id]
        ancestors = {current.id}
        while current.parent_id is not None:
            current = by_id[current.parent_id]
            ancestors.add(current.id)
        return ancestors

    def read_subtree_ids(self, topic_ids: list[str]) -> set[str]:
        subtree = set(topic_ids)
        frontier = set(topic_ids)
        while frontier:
            frontier = {topic.id for topic in self.topics if topic.parent_id in frontier} - subtree
            subtree |= frontier
        return subtree

    # A topic is trashed when it or a topic above it has that status.
    # Only the topic that the user discarded holds the status.
    # When the user restores that topic, JRI gives the whole subtree back.
    def read_trashed_ids(self) -> set[str]:
        by_id = {topic.id: topic for topic in self.topics}
        trashed: set[str] = set()
        for topic in self.topics:
            current: Topic | None = topic
            while current is not None:
                if current.status == "trashed":
                    trashed.add(topic.id)
                    break
                current = by_id[current.parent_id] if current.parent_id is not None else None
        return trashed

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
        if OVERVIEW_TOPIC_ID not in topic_ids:
            raise ValueError(f"The overview topic `{OVERVIEW_TOPIC_ID}` must exist.")
        names = [topic.name.strip().casefold() for topic in self.topics]
        if len(names) != len(set(names)):
            raise ValueError("Topic names must be unique.")
        if any(note.topic_id not in topic_ids for note in self.notes):
            raise ValueError("Every note must reference an existing topic.")
        if any(int(note.id[1:]) >= int(self.next_note_id[1:]) for note in self.notes):
            raise ValueError(f"Note IDs must come before `{self.next_note_id}`.")
        triples = [(item.source_id, item.target_id, item.label) for item in self.connections]
        if len(triples) != len(set(triples)):
            raise ValueError("Connections must be unique.")
        note_ids = {note.id for note in self.notes}
        for connection in self.connections:
            if connection.source_id not in note_ids or connection.target_id not in note_ids:
                raise ValueError("Connection endpoints must reference existing notes.")
        self._validate_tree()
        return self

    def _validate_tree(self) -> None:
        by_id = {topic.id: topic for topic in self.topics}
        rootless = [topic.id for topic in self.topics if topic.parent_id is None]
        if rootless != [OVERVIEW_TOPIC_ID]:
            raise ValueError(f"Only the overview topic `{OVERVIEW_TOPIC_ID}` stands without a parent topic.")
        if any(topic.parent_id is not None and topic.parent_id not in by_id for topic in self.topics):
            raise ValueError("Every topic must reference an existing parent topic.")
        for topic in self.topics:
            depth = 1
            visited = {topic.id}
            current = topic
            while current.parent_id is not None:
                if current.parent_id in visited:
                    raise ValueError(f"Topic `{topic.id}` cannot stand inside itself.")
                visited.add(current.parent_id)
                current = by_id[current.parent_id]
                depth += 1
            if depth > MAX_DEPTH:
                raise ValueError(f"Topics nest {MAX_DEPTH} levels deep at most.")


class ReadQuery(BaseModel):
    text: str | None = None
    ids: list[NoteId] | None = None
    topic_ids: list[TopicId] | None = None
    traverse_from: list[NoteId] | None = None
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
    def __init__(self, path: Path, project_name: str) -> None:
        self.path = path
        self.project_name = project_name
        self.graph = self._load()
        logger.info("initialized notes=%d connections=%d", len(self.graph.notes), len(self.graph.connections))

    @property
    def initial_topic(self) -> Topic:
        return self.graph.read_overview()

    @property
    def trashed_topic_ids(self) -> set[str]:
        return self.graph.read_trashed_ids()

    def read(self, query: ReadQuery) -> tuple[list[Note], list[Connection]]:
        topic_ids = {topic.id for topic in self.graph.topics}
        if not set(query.topic_ids or []) <= topic_ids:
            raise ValueError(f"Unknown topic `{min(set(query.topic_ids or []) - topic_ids)}`.")
        # JRI reads a named topic with all the topics below it.
        # JRI also reads a named topic that is trashed, because the model must see what it would restore.
        allowed_topics = (
            self.graph.read_subtree_ids(query.topic_ids) if query.topic_ids else topic_ids - self.trashed_topic_ids
        )
        by_id = {note.id: note for note in self.graph.notes}
        candidates = {note.id: note for note in self.graph.notes if note.topic_id in allowed_topics}
        unfiltered = query.text is None and not query.ids and not query.traverse_from
        selected = dict(candidates) if unfiltered else {}
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

        selected.update(self._traverse(query, by_id))

        selected = {note_id: note for note_id, note in selected.items() if note.topic_id in allowed_topics}
        selected_ids = set(selected)
        connections = [
            item for item in self.graph.connections if item.source_id in selected_ids and item.target_id in selected_ids
        ]
        logger.info("read_finished notes=%d connections=%d", len(selected), len(connections))
        return list(selected.values()), connections

    def add(self, texts: list[str], topic_id: str) -> list[Note]:
        if not texts or any(not text.strip() for text in texts):
            raise ValueError("Provide one or more non-blank note texts.")
        graph = self.graph.model_copy(deep=True)
        self._find_topic(graph, topic_id)
        next_number = int(graph.next_note_id[1:])
        added = [Note(id=f"n{next_number + index}", topic_id=topic_id, text=text) for index, text in enumerate(texts)]
        graph.notes.extend(added)
        graph.next_note_id = f"n{next_number + len(added)}"
        self._save(graph)
        logger.info("add_finished ids=%r", [note.id for note in added])
        return added

    def add_topic(self, name: str, parent_id: str, summary: str) -> Topic:
        if not name.strip():
            raise ValueError("Topic name cannot be blank.")
        if not summary.strip():
            raise ValueError("Topic summary cannot be blank.")
        graph = self.graph.model_copy(deep=True)
        if any(topic.name.strip().casefold() == name.strip().casefold() for topic in graph.topics):
            raise ValueError(f"Topic `{name.strip()}` already exists.")
        self._find_topic(graph, parent_id)
        next_number = max(int(topic.id[1:]) for topic in graph.topics) + 1
        topic = Topic(
            id=f"t{next_number}", parent_id=parent_id, name=name.strip(), summary=summary.strip(), status="open"
        )
        graph.topics.append(topic)
        self._save(graph)
        logger.info("add_topic_finished topic_id=%s parent_id=%s", topic.id, parent_id)
        return topic

    def find_topic(self, value: str) -> Topic | None:
        for topic in self.graph.topics:
            if topic.id == value or topic.name.strip().casefold() == value.strip().casefold():
                return topic
        return None

    def update_topic(
        self,
        topic_id: str,
        status: Literal["open", "done"] | None = None,
        summary: str | None = None,
        name: str | None = None,
        parent_id: str | None = None,
    ) -> Topic:
        graph = self.graph.model_copy(deep=True)
        if summary is not None and not summary.strip():
            raise ValueError("Topic summary cannot be blank.")
        if name is not None and not name.strip():
            raise ValueError("Topic name cannot be blank.")
        topic = self._find_topic(graph, topic_id)
        if name is not None and any(
            item.id != topic.id and item.name.strip().casefold() == name.strip().casefold() for item in graph.topics
        ):
            raise ValueError(f"Topic `{name.strip()}` already exists.")
        if parent_id is not None:
            if topic.id == OVERVIEW_TOPIC_ID:
                raise ValueError(f"The overview topic `{topic.id}` cannot stand under another topic.")
            self._find_topic(graph, parent_id)
            if parent_id in graph.read_trashed_ids():
                raise ValueError(f"Topic `{parent_id}` is trashed. Restore it before standing a topic under it.")
            topic.parent_id = parent_id
        if status is not None:
            topic.status = status
        if summary is not None:
            topic.summary = summary.strip()
        if name is not None:
            topic.name = name.strip()
        self._save(graph)
        logger.info("update_topic_finished topic_id=%s status=%s", topic.id, topic.status)
        return topic

    def edit(self, note_id: str, text: str) -> Note:
        if not text.strip():
            raise ValueError("Note text cannot be blank.")
        graph = self.graph.model_copy(deep=True)
        note = self._find_note(graph, note_id)
        note.text = text
        self._save(graph)
        logger.info("edit_finished note_id=%s", note.id)
        return note

    def move(self, note_ids: list[str], topic_id: str) -> list[str]:
        if not note_ids or len(note_ids) != len(set(note_ids)):
            raise ValueError("Provide one or more unique note IDs.")
        graph = self.graph.model_copy(deep=True)
        self._find_topic(graph, topic_id)
        if topic_id in graph.read_trashed_ids():
            raise ValueError(f"Topic `{topic_id}` is trashed. Restore it before moving notes into it.")
        for note_id in note_ids:
            self._find_note(graph, note_id).topic_id = topic_id
        self._save(graph)
        logger.info("move_finished note_ids=%r topic_id=%s", note_ids, topic_id)
        return note_ids

    # The conversation that made a note stays, and a rewind writes the note again.
    def delete(self, note_ids: list[str]) -> list[str]:
        if not note_ids or len(note_ids) != len(set(note_ids)):
            raise ValueError("Provide one or more unique note IDs.")
        graph = self.graph.model_copy(deep=True)
        for note_id in note_ids:
            self._find_note(graph, note_id)
        discarded = set(note_ids)
        graph.notes = [note for note in graph.notes if note.id not in discarded]
        graph.connections = [
            item for item in graph.connections if item.source_id not in discarded and item.target_id not in discarded
        ]
        self._save(graph)
        logger.info("delete_finished note_ids=%r", note_ids)
        return note_ids

    # A trashed topic keeps its status until the user restores it.
    def trash(self, topic_ids: list[str]) -> list[str]:
        if not topic_ids or len(topic_ids) != len(set(topic_ids)):
            raise ValueError("Provide one or more unique topic IDs.")
        if OVERVIEW_TOPIC_ID in topic_ids:
            raise ValueError(f"The overview topic `{OVERVIEW_TOPIC_ID}` cannot be trashed.")
        graph = self.graph.model_copy(deep=True)
        for topic_id in topic_ids:
            self._find_topic(graph, topic_id).status = "trashed"
        self._save(graph)
        logger.info("trash_finished topic_ids=%r", topic_ids)
        return topic_ids

    def connect(self, connections: list[Connection]) -> int:
        if not connections:
            raise ValueError("Provide one or more connections.")
        graph = self.graph.model_copy(deep=True)
        existing = {(item.source_id, item.target_id, item.label) for item in graph.connections}
        requested = [(item.source_id, item.target_id, item.label) for item in connections]
        if len(requested) != len(set(requested)):
            raise ValueError("Connections in a request must be unique.")
        for connection in connections:
            self._find_note(graph, connection.source_id)
            self._find_note(graph, connection.target_id)
            if not connection.label.strip():
                raise ValueError("Connection labels cannot be blank.")
            if (connection.source_id, connection.target_id, connection.label) not in existing:
                graph.connections.append(connection)
        self._save(graph)
        count = len(set(requested) - existing)
        logger.info("connect_finished count=%d", count)
        return count

    def disconnect(self, connections: list[Connection]) -> int:
        if not connections:
            raise ValueError("Provide one or more connections.")
        graph = self.graph.model_copy(deep=True)
        requested = {(item.source_id, item.target_id, item.label) for item in connections}
        if len(requested) != len(connections):
            raise ValueError("Connections in a request must be unique.")
        for connection in connections:
            self._find_note(graph, connection.source_id)
            self._find_note(graph, connection.target_id)
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

    def restore(self, graph: Graph, *, reuse_note_ids: bool = False) -> None:
        # Copy the graph deeply. A caller must not change the restored notebook through its checkpoint copy.
        restored = graph.model_copy(deep=True)
        # Do not use a note ID again.
        # Keep the highest ID that JRI gave, unless the caller writes the removed notes again.
        if not reuse_note_ids and int(restored.next_note_id[1:]) < int(self.graph.next_note_id[1:]):
            restored.next_note_id = self.graph.next_note_id
        self._save(restored)

    def render(self, topic_id: TopicId) -> str:
        return self._dump(self.graph, topic_id)

    # A trashed topic holds thinking that the user discarded.
    # Do not put it, its subtree, their notes, or their connections in a document for another model.
    @classmethod
    def exclude_trashed(cls, document: bytes) -> str:
        if not document:
            return ""
        try:
            graph = cls._parse(safe_load(document))
            kept_topic_ids = {topic.id for topic in graph.topics} - graph.read_trashed_ids()
            notes = [note for note in graph.notes if note.topic_id in kept_topic_ids]
            visible = {note.id for note in notes}
            remaining = Graph(
                topics=[topic for topic in graph.topics if topic.id in kept_topic_ids],
                notes=notes,
                connections=[
                    item for item in graph.connections if item.source_id in visible and item.target_id in visible
                ],
                next_note_id=graph.next_note_id,
            )
        except (AttributeError, KeyError, OSError, TypeError, ValueError, YAMLError) as error:
            logger.exception("document_parse_failed")
            raise PersistenceError("The notebook document cannot be read.") from error
        else:
            return cls._dump(remaining)

    def _traverse(self, query: ReadQuery, by_id: dict[str, Note]) -> dict[str, Note]:
        frontier = set(query.traverse_from or [])
        unknown = frontier - by_id.keys()
        if unknown:
            raise ValueError(f"Unknown note `{min(unknown)}`.")
        reached = {note_id: by_id[note_id] for note_id in frontier}
        visited = set(frontier)
        direction = query.direction or "both"
        for _ in range(query.depth or 1):
            next_frontier: set[str] = set()
            for connection in self.graph.connections:
                if direction in {"outgoing", "both"} and connection.source_id in frontier:
                    next_frontier.add(connection.target_id)
                if direction in {"incoming", "both"} and connection.target_id in frontier:
                    next_frontier.add(connection.source_id)
            next_frontier -= visited
            reached.update((note_id, by_id[note_id]) for note_id in next_frontier)
            visited |= next_frontier
            frontier = next_frontier
        return reached

    def _load(self) -> Graph:
        if not self.path.exists():
            # The project has no name of its own yet, so it starts with the name of its directory.
            graph = Graph(topics=[Topic(id=OVERVIEW_TOPIC_ID, name=self.project_name, status="open")])
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

    # The document is the root topic. Its own fields are at the top, and its children nest below them.
    @staticmethod
    def _parse(data: dict[str, Any]) -> Graph:
        topics: list[dict[str, Any]] = []
        notes: list[dict[str, Any]] = []
        pending = [({key: value for key, value in data.items() if key not in {"connections", "next_note_id"}}, None)]
        while pending:
            node, parent_id = pending.pop()
            subtopics = node.pop("topics", [])
            for note_id, text in node.pop("notes").items():
                notes.append({"id": note_id, "topic_id": node["id"], "text": text})
            topics.append({**node, "parent_id": parent_id})
            pending.extend((subtopic, node["id"]) for subtopic in subtopics)
        topics.sort(key=lambda topic: int(topic["id"][1:]))
        notes.sort(key=lambda note: int(note["id"][1:]))
        connections = []
        for value in data["connections"]:
            source_id, label_and_target = value.split(" ", maxsplit=1)
            label, target_id = label_and_target.rsplit(" ", maxsplit=1)
            connections.append({"source_id": source_id, "target_id": target_id, "label": label})
        return Graph.model_validate({
            "topics": topics,
            "notes": notes,
            "connections": connections,
            "next_note_id": data["next_note_id"],
        })

    def _save(self, graph: Graph) -> None:
        graph = Graph.model_validate(graph)
        self._write(graph)
        self.graph = graph
        logger.debug("saved notes=%d connections=%d", len(graph.notes), len(graph.connections))

    def _write(self, graph: Graph) -> None:
        try:
            files.write_atomically(self.path, self._dump(graph))
        except OSError as error:
            logger.exception("file_write_failed path=%r", self.path)
            raise PersistenceError(f"Could not save the notebook file `{self.path}`: {error.strerror}") from error

    # A pinned document is the whole document, but JRI removes some parts from it.
    # It removes the notes of the topics that are not on the active branch, and every trashed subtree.
    # It also removes the connections that reach no rendered note, and the next note ID.
    @staticmethod
    def _dump(graph: Graph, topic_id: TopicId | None = None) -> str:
        # The walk stops at a trashed topic, so that topic renders nothing.
        # The ancestors of the active topic that are below it render nothing either.
        # Take them out of the pinned set. The set then names the topics whose notes the document holds.
        pinned = None if topic_id is None else graph.read_ancestor_ids(topic_id) - graph.read_trashed_ids()
        rendered = {note.id for note in graph.notes if pinned is None or note.topic_id in pinned}
        data = Notebook._build_topic(graph.read_overview(), graph, graph.read_subtopics(), pinned)
        data["connections"] = [
            f"{item.source_id} {item.label} {item.target_id}"
            for item in graph.connections
            if item.source_id in rendered or item.target_id in rendered
        ]
        if topic_id is None:
            data["next_note_id"] = graph.next_note_id
        return safe_dump(data, sort_keys=False, allow_unicode=True, width=10**9)

    @staticmethod
    def _build_topic(
        topic: Topic, graph: Graph, subtopics: dict[str, list[Topic]], pinned: set[str] | None
    ) -> dict[str, Any]:
        # The nesting shows the position of the topic. The field that holds the position adds nothing here.
        data: dict[str, Any] = topic.model_dump(exclude_none=True, exclude={"parent_id"})
        if pinned is None or topic.id in pinned:
            data["notes"] = {note.id: note.text for note in graph.notes if note.topic_id == topic.id}
        # The walk stops at a trashed topic, and the document holds no topic below it.
        children = [
            Notebook._build_topic(child, graph, subtopics, pinned)
            for child in subtopics.get(topic.id, [])
            if pinned is None or child.status != "trashed"
        ]
        if children:
            data["topics"] = children
        return data

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
