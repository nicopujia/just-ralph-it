from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from yaml import safe_load

from jri.core.exceptions import PersistenceError
from jri.core.notes import Connection, Graph, Notebook, ReadQuery

MAX_SEARCH_RESULTS = 10
VALID_GRAPH: dict[str, Any] = {
    "topics": [{"id": "t1", "name": "Overview", "status": "open"}],
    "notes": [{"id": "n1", "topic_id": "t1", "text": "A requirement"}],
    "connections": [],
}


def test_advances_topic_and_note_ids_independently(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")

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
        {
            **VALID_GRAPH,
            "topics": [{"id": "t2", "name": "Overview", "status": "open"}],
            "notes": [{"id": "n1", "topic_id": "t2", "text": "A requirement"}],
        },
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
def test_rejects_invalid_graph_data(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Graph.model_validate(data)


def test_rejects_an_invalid_connection_batch_without_changing_anything(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    notebook.add(["First", "Second"], "t1")
    before = notebook.graph.model_copy(deep=True)

    with pytest.raises(ValueError, match="n99"):
        notebook.connect([
            Connection(source_id="n1", target_id="n2", label="requires"),
            Connection(source_id="n1", target_id="n99", label="requires"),
        ])

    assert notebook.graph == before
    assert Notebook(notebook.path).graph == before


def test_removes_the_connections_of_a_deleted_note(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
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


def test_restores_notebook_changes_after_restart(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    topic = notebook.add_topic("Delivery")
    first, second = notebook.add(["Deploy manually.", "Use the main branch 🚀."], topic.id)
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
        (second.id, "Use the main branch 🚀."),
    ]
    assert notebook.graph.connections == [connection]

    assert notebook.disconnect([connection]) == 1
    assert Notebook(notebook.path).graph.connections == []


def test_stores_notes_in_a_compact_schema(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    delivery = notebook.add_topic("Delivery")
    first = notebook.add(["First"], "t1")[0]
    second = notebook.add(["Second 🚀"], delivery.id)[0]
    third = notebook.add(["Third"], "t1")[0]
    notebook.update_topic(delivery.id, "done", "Delivery is fully defined.")
    notebook.connect([
        Connection(source_id=first.id, target_id=second.id, label="supports"),
        Connection(source_id=first.id, target_id=third.id, label="strongly supports"),
    ])

    data = safe_load(notebook.path.read_text())

    assert data == {
        "topics": [
            {"id": "t1", "name": "Project overview", "status": "open", "notes": {"n1": "First", "n3": "Third"}},
            {
                "id": "t2",
                "name": "Delivery",
                "status": "done",
                "summary": "Delivery is fully defined.",
                "notes": {"n2": "Second 🚀"},
            },
        ],
        "connections": ["n1 supports n2", "n1 strongly supports n3"],
    }
    assert Notebook(notebook.path).graph == notebook.graph


def test_renders_only_visible_topics_and_relevant_notes(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    delivery = notebook.add_topic("Delivery")
    security = notebook.add_topic("Security")
    discarded = notebook.add_topic("Discarded")
    notebook.add(["Overview"], "t1")
    notebook.add(["Deploy automatically."], delivery.id)
    notebook.add(["Encrypt credentials."], security.id)
    notebook.add(["Do not build this."], discarded.id)
    notebook.update_topic(discarded.id, "trashed")
    notebook.connect([Connection(source_id="n1", target_id="n2", label="supports")])

    delivery_context = safe_load(notebook.render(delivery.id))
    security_context = safe_load(notebook.render(security.id))

    assert delivery_context == {
        "topics": [
            {"id": "t1", "name": "Project overview", "status": "open", "notes": {"n1": "Overview"}},
            {"id": "t2", "name": "Delivery", "status": "open", "notes": {"n2": "Deploy automatically."}},
            {"id": "t3", "name": "Security", "status": "open"},
        ]
    }
    assert security_context["topics"][1].get("notes") is None
    assert security_context["topics"][2]["notes"] == {"n3": "Encrypt credentials."}


def test_renders_an_empty_overview(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")

    assert safe_load(notebook.render("t1")) == {
        "topics": [{"id": "t1", "name": "Project overview", "status": "open", "notes": {}}]
    }


@pytest.mark.parametrize(
    ("query", "expected_ids"),
    [
        (ReadQuery(traverse_from=["n1"], direction="outgoing", depth=1), {"n1", "n2"}),
        (ReadQuery(traverse_from=["n1"], direction="outgoing", depth=2), {"n1", "n2", "n3"}),
        (ReadQuery(traverse_from=["n1"], direction="incoming", depth=1), {"n1", "n3"}),
        (ReadQuery(traverse_from=["n3"], direction="incoming", depth=1), {"n2", "n3"}),
        (ReadQuery(traverse_from=["n1"]), {"n1", "n2", "n3"}),
    ],
    ids=["outgoing", "outgoing-deeper", "incoming", "incoming-from-the-end", "both-by-default"],
)
def test_respects_traversal_direction_and_depth(tmp_path: Path, query: ReadQuery, expected_ids: set[str]) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    notebook.add(["First", "Second", "Third"], "t1")
    notebook.connect([
        Connection(source_id="n1", target_id="n2", label="requires"),
        Connection(source_id="n2", target_id="n3", label="requires"),
        Connection(source_id="n3", target_id="n1", label="closes"),
    ])

    notes, _ = notebook.read(query)

    assert {note.id for note in notes} == expected_ids


def test_ranks_fuzzy_matches_and_caps_the_result(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    notebook.add([*(f"Unrelated note {index}" for index in range(12)), "Deploy from the main branch."], "t1")

    notes, _ = notebook.read(ReadQuery(text="MAIN BRANCH"))

    assert notes[0].text == "Deploy from the main branch."
    assert len(notes) == MAX_SEARCH_RESULTS


def test_searches_only_visible_topics(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    topic = notebook.add_topic("Discarded idea")
    notebook.add(["Deploy from the main branch."], topic.id)
    notebook.update_topic(topic.id, "trashed")

    assert notebook.read(ReadQuery(text="main branch"))[0] == []
    assert [note.text for note in notebook.read(ReadQuery(text="main branch", topic_ids=[topic.id]))[0]] == [
        "Deploy from the main branch."
    ]


@pytest.mark.parametrize(
    "query", [{"text": "  "}, {"depth": 0}, {"traverse_from": ["n1"], "depth": -1}], ids=["blank-text", "zero", "under"]
)
def test_rejects_invalid_search_selectors(query: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ReadQuery(**query)


def test_rejects_unknown_selectors(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    notebook.add(["First"], "t1")

    with pytest.raises(ValueError, match="t99"):
        notebook.read(ReadQuery(topic_ids=["t99"]))
    with pytest.raises(ValueError, match="n99"):
        notebook.read(ReadQuery(ids=["n99"]))
    with pytest.raises(ValueError, match="n99"):
        notebook.read(ReadQuery(traverse_from=["n99"]))


def test_hides_trashed_topics_unless_selected(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    topic = notebook.add_topic("Discarded idea")
    notebook.add(["Do not show this by default."], topic.id)
    notebook.update_topic(topic.id, "trashed")

    visible, _ = notebook.read(ReadQuery())
    selected, _ = notebook.read(ReadQuery(topic_ids=[topic.id]))

    assert visible == []
    assert [note.id for note in selected] == ["n1"]


@pytest.mark.parametrize(
    "contents",
    [
        ": invalid yaml",
        "topics: []",
        "topics:\n- id: t1\n  name: Overview\n  status: open\n  notes: []\nconnections: []",
        "topics:\n- id: t1\n  name: Overview\n  status: open\n  notes: {n1: First}\nconnections: [n1 malformed]",
        (
            "topics:\n- id: t1\n  name: Overview\n  status: open\n  notes: {n1: First}\n"
            "connections: [n1 supports n1, n1 supports n1]"
        ),
        "topics:\n- id: t1\n  name: Overview\n  status: open\n  notes: {n1: First}\nconnections: [n1 supports n2]",
    ],
)
def test_explains_how_to_reset_an_invalid_notebook_file(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "notebook.yaml"
    path.write_text(contents)

    with pytest.raises(PersistenceError, match="--force"):
        Notebook(path)


def test_reports_a_notebook_that_cannot_be_written(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    notebook.path.unlink()
    notebook.path.mkdir()
    (notebook.path / "blocker").write_text("taken")

    with pytest.raises(PersistenceError, match="Could not save the notebook file"):
        notebook.add(["A requirement"], "t1")

    assert sorted(path.name for path in tmp_path.iterdir()) == ["notebook.yaml"]
