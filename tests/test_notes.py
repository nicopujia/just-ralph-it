from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jri.core.exceptions import PersistenceError
from jri.core.notes import Connection, Graph, Notebook, ReadQuery

VALID_GRAPH: dict[str, Any] = {
    "overview_topic_id": "t1",
    "topics": [{"id": "t1", "name": "Overview", "status": "open"}],
    "notes": [{"id": "n1", "topic_id": "t1", "text": "A requirement"}],
    "connections": [],
}


def test_topic_and_note_ids_advance_independently(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "graph.json")

    assert [note.id for note in notebook.add(["one", "two", "three"], "t1")] == ["n1", "n2", "n3"]
    assert notebook.add_topic("Second topic").id == "t2"
    assert [notebook.add_topic(name).id for name in ("Third topic", "Fourth topic")] == ["t3", "t4"]

    notebook = Notebook(notebook.path)

    assert notebook.add(["four"], "t1")[0].id == "n4"
    assert notebook.add_topic("Fifth topic").id == "t5"


@pytest.mark.parametrize(
    "data",
    [
        {**VALID_GRAPH, "topics": [{"id": "n1", "name": "Overview", "status": "open"}]},
        {
            **VALID_GRAPH,
            "notes": [
                {"id": "n1", "topic_id": "t1", "text": "First"},
                {"id": "n1", "topic_id": "t1", "text": "Second"},
            ],
        },
        {
            **VALID_GRAPH,
            "topics": [
                {"id": "t1", "name": "Overview", "status": "open"},
                {"id": "t2", "name": " overview ", "status": "open"},
            ],
        },
        {**VALID_GRAPH, "overview_topic_id": "t2"},
        {**VALID_GRAPH, "notes": [{"id": "n1", "topic_id": "t2", "text": "A requirement"}]},
        {**VALID_GRAPH, "connections": [{"source_id": "n1", "target_id": "n2", "label": "requires"}]},
        {
            **VALID_GRAPH,
            "connections": [
                {"source_id": "t1", "target_id": "n1", "label": "contains"},
                {"source_id": "t1", "target_id": "n1", "label": "contains"},
            ],
        },
        {**VALID_GRAPH, "topics": [{"id": "t1", "name": " ", "status": "open"}]},
    ],
    ids=[
        "malformed-id",
        "duplicate-id",
        "duplicate-topic-name",
        "missing-overview",
        "missing-note-topic",
        "dangling-connection",
        "duplicate-connection",
        "blank-content",
    ],
)
def test_graph_rejects_invalid_data(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Graph.model_validate(data)


def test_invalid_connection_batch_changes_nothing(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "graph.json")
    notebook.add(["First", "Second"], "t1")
    before = notebook.graph.model_copy(deep=True)

    with pytest.raises(ValueError, match="n99"):
        notebook.connect([
            Connection(source_id="n1", target_id="n2", label="requires"),
            Connection(source_id="n1", target_id="n99", label="requires"),
        ])

    assert notebook.graph == before
    assert Notebook(notebook.path).graph == before


def test_deleting_note_removes_its_connections(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "graph.json")
    notebook.add(["First", "Second", "Third"], "t1")
    notebook.connect([
        Connection(source_id="n1", target_id="n2", label="requires"),
        Connection(source_id="n2", target_id="n3", label="requires"),
        Connection(source_id="n1", target_id="n3", label="supports"),
    ])

    notebook.delete(["n2"])
    graph = Notebook(notebook.path).graph

    assert {note.id for note in graph.notes} == {"n1", "n3"}
    assert {(item.source_id, item.target_id, item.label) for item in graph.connections} == {("n1", "n3", "supports")}


def test_notebook_changes_survive_restart(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "graph.json")
    topic = notebook.add_topic("Delivery")
    first, second = notebook.add(["Deploy manually.", "Use the main branch."], topic.id)
    connection = Connection(source_id=first.id, target_id=second.id, label="requires")
    notebook.connect([connection])

    notebook.edit(first.id, "Deploy automatically.")
    notebook.update_topic(topic.id, "done", "Delivery is fully defined.")

    notebook = Notebook(notebook.path)
    restored_topic = notebook.find_topic(topic.id)
    assert restored_topic is not None
    assert (restored_topic.status, restored_topic.summary) == ("done", "Delivery is fully defined.")
    assert [(note.id, note.text) for note in notebook.graph.notes] == [
        (first.id, "Deploy automatically."),
        (second.id, "Use the main branch."),
    ]
    assert notebook.graph.connections == [connection]

    assert notebook.disconnect([connection]) == 1
    assert Notebook(notebook.path).graph.connections == []


@pytest.mark.parametrize(
    ("query", "expected_ids"),
    [
        (ReadQuery(traverse_from=["n1"], direction="outgoing", depth=1), {"n1", "n2"}),
        (ReadQuery(traverse_from=["n1"], direction="outgoing", depth=2), {"n1", "n2", "n3"}),
        (ReadQuery(traverse_from=["n3"], direction="incoming", depth=1), {"n2", "n3"}),
    ],
)
def test_read_respects_traversal_direction_and_depth(tmp_path: Path, query: ReadQuery, expected_ids: set[str]) -> None:
    notebook = Notebook(tmp_path / "graph.json")
    notebook.add(["First", "Second", "Third"], "t1")
    notebook.connect([
        Connection(source_id="n1", target_id="n2", label="requires"),
        Connection(source_id="n2", target_id="n3", label="requires"),
    ])

    notes, _ = notebook.read(query)

    assert {note.id for note in notes} == expected_ids


def test_read_hides_trashed_topics_unless_selected(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "graph.json")
    topic = notebook.add_topic("Discarded idea")
    notebook.add(["Do not show this by default."], topic.id)
    notebook.update_topic(topic.id, "trashed")

    visible, _ = notebook.read(ReadQuery())
    selected, _ = notebook.read(ReadQuery(topic_ids=[topic.id]))

    assert visible == []
    assert [note.id for note in selected] == ["n1"]


def test_invalid_graph_file_explains_how_to_reset(tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    path.write_text("not json")

    with pytest.raises(PersistenceError, match="--force"):
        Notebook(path)
