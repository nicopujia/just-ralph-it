from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from yaml import safe_load

from jri.core.exceptions import PersistenceError
from jri.core.notes import Connection, Graph, Note, Notebook, ReadQuery, Topic

type Change = Callable[[Notebook], object]

BLANK_LABEL_CONNECTION = Connection(source_id="n1", target_id="n2", label=" ")
CONNECTION = Connection(source_id="n1", target_id="n2", label="requires")
MAX_SEARCH_RESULTS = 10
VALID_GRAPH: dict[str, Any] = {
    "topics": [{"id": "t1", "name": "Overview", "status": "open"}],
    "notes": [{"id": "n1", "topic_id": "t1", "text": "A requirement"}],
    "connections": [],
    "next_note_id": "n2",
}


def test_advances_topic_and_note_ids_independently(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")

    assert [note.id for note in notebook.add(["one", "two", "three"], "t1")] == ["n1", "n2", "n3"]
    assert notebook.add_topic("Second topic").id == "t2"
    assert [notebook.add_topic(name).id for name in ("Third topic", "Fourth topic")] == ["t3", "t4"]

    notebook = Notebook(notebook.path)

    assert notebook.add(["four"], "t1")[0].id == "n4"
    assert notebook.add_topic("Fifth topic").id == "t5"


# JRI only appends a topic, so a gap between topic IDs can come from a hand-edited file. A count of the topics
# would allocate an ID that a later topic already holds.
def test_allocates_a_topic_id_after_the_highest_one_in_the_file(tmp_path: Path) -> None:
    path = tmp_path / "notebook.yaml"
    path.write_text(
        "topics:\n- id: t1\n  name: Overview\n  status: open\n  notes: {n1: First}\n"
        "- id: t3\n  name: Delivery\n  status: open\n  notes: {}\n"
        "connections: []\nnext_note_id: n2",
        encoding="utf-8",
    )

    assert Notebook(path).add_topic("Security").id == "t4"


@pytest.mark.parametrize(
    "data",
    [
        {
            **VALID_GRAPH,
            "topics": [
                {"id": "t1", "name": "Overview", "status": "open"},
                {"id": "tx", "name": "Delivery", "status": "open"},
            ],
        },
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
        {**VALID_GRAPH, "next_note_id": "n1"},
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
        "taken-next-id",
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


def test_never_reuses_the_id_of_a_deleted_note(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    notebook.add(["First", "Second", "Third"], "t1")

    notebook.delete(["n3"])

    assert notebook.add(["Fourth"], "t1")[0].id == "n4"
    assert Notebook(notebook.path).add(["Fifth"], "t1")[0].id == "n5"


def test_keeps_the_next_note_id_when_restoring_an_earlier_graph(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    notebook.add(["First"], "t1")
    checkpoint = notebook.graph.model_copy(deep=True)
    notebook.add(["Second"], "t1")

    notebook.restore(checkpoint)

    assert [note.id for note in notebook.graph.notes] == ["n1"]
    assert notebook.add(["Third"], "t1")[0].id == "n3"


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


# The file stores each note under its topic in the order the graph holds them, so a load that sorts the IDs as
# text puts `n10` before `n2` and the next save rewrites the user's notebook in that order.
def test_keeps_notes_in_the_order_they_were_added_after_a_restart(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    added = [note.id for note in notebook.add([f"Note {index}" for index in range(12)], "t1")]

    notebook = Notebook(notebook.path)
    notebook.add(["One more"], "t1")

    assert [note.id for note in notebook.graph.notes] == [*added, "n13"]
    assert list(safe_load(notebook.path.read_text(encoding="utf-8"))["topics"][0]["notes"]) == [*added, "n13"]


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

    assert notebook.path.read_text(encoding="utf-8") == (
        "topics:\n"
        "- id: t1\n"
        "  name: Project overview\n"
        "  status: open\n"
        "  notes:\n"
        "    n1: First\n"
        "    n3: Third\n"
        "- id: t2\n"
        "  name: Delivery\n"
        "  status: done\n"
        "  summary: Delivery is fully defined.\n"
        "  notes:\n"
        "    n2: Second 🚀\n"
        "connections:\n"
        "- n1 supports n2\n"
        "- n1 strongly supports n3\n"
        "next_note_id: n4\n"
    )
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
        ],
        "connections": ["n1 supports n2"],
    }
    assert security_context["topics"][1].get("notes") is None
    assert security_context["topics"][2]["notes"] == {"n3": "Encrypt credentials."}


def test_excludes_a_trashed_topic_from_a_document(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    discarded = notebook.add_topic("Discarded")
    notebook.add(["Overview"], "t1")
    notebook.add(["Do not build this."], discarded.id)
    notebook.connect([Connection(source_id="n1", target_id="n2", label="supersedes")])
    notebook.update_topic(discarded.id, "trashed")

    assert safe_load(Notebook.exclude_trashed(notebook.path.read_bytes())) == {
        "topics": [{"id": "t1", "name": "Project overview", "status": "open", "notes": {"n1": "Overview"}}],
        "connections": [],
        "next_note_id": "n3",
    }


def test_leaves_a_document_holding_no_trashed_topic_as_it_stands(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    notebook.add(["Overview"], "t1")
    notebook.update_topic(notebook.add_topic("Delivery").id, "done", "Settled.")

    assert Notebook.exclude_trashed(notebook.path.read_bytes()) == notebook.path.read_text()


def test_reads_a_notebook_never_written_as_an_empty_document() -> None:
    assert not Notebook.exclude_trashed(b"")


def test_reports_a_notebook_document_that_cannot_be_read() -> None:
    with pytest.raises(PersistenceError, match="cannot be read"):
        Notebook.exclude_trashed(b"nonsense")


def test_renders_an_empty_overview(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")

    assert safe_load(notebook.render("t1")) == {
        "topics": [{"id": "t1", "name": "Project overview", "status": "open", "notes": {}}],
        "connections": [],
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
        "a bare string",
        "topics: []",
        "topics:\n- id: t1\n  name: Overview\n  status: open\n  notes: []\nconnections: []\nnext_note_id: n1",
        (
            "topics:\n- id: t1\n  name: Overview\n  status: open\n  notes: {n1: First}\n"
            "connections: [n1 malformed]\nnext_note_id: n2"
        ),
        (
            "topics:\n- id: t1\n  name: Overview\n  status: open\n  notes: {n1: First}\n"
            "connections: [n1 supports n1, n1 supports n1]\nnext_note_id: n2"
        ),
        (
            "topics:\n- id: t1\n  name: Overview\n  status: open\n  notes: {n1: First}\n"
            "connections: [n1 supports n2]\nnext_note_id: n2"
        ),
        "topics:\n- id: t1\n  name: Overview\n  status: open\n  notes: {n1: First}\nconnections: []",
        "topics:\n- id: t1\n  name: Overview\n  status: open\n  notes: {n1: First}\nconnections: []\nnext_note_id: n1",
    ],
    ids=[
        "malformed-yaml",
        "not-a-mapping",
        "truncated",
        "notes-not-a-mapping",
        "malformed-connection",
        "duplicate-connection",
        "dangling-connection",
        "no-next-id",
        "taken-next-id",
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


def test_keeps_the_last_saved_graph_in_memory_when_writing_fails(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    notebook.add(["First"], "t1")
    before = notebook.graph.model_copy(deep=True)
    notebook.path.unlink()
    notebook.path.mkdir()
    (notebook.path / "blocker").write_text("taken")

    with pytest.raises(PersistenceError):
        notebook.add(["Second"], "t1")

    assert notebook.graph == before


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda notebook: notebook.add([], "t1"), "non-blank note texts"),
        (lambda notebook: notebook.add([" "], "t1"), "non-blank note texts"),
        (lambda notebook: notebook.add(["Third"], "t9"), "Unknown topic `t9`"),
        (lambda notebook: notebook.add_topic(" "), "Topic name cannot be blank"),
        (lambda notebook: notebook.add_topic("  project OVERVIEW "), "already exists"),
        (lambda notebook: notebook.update_topic("t1", "open", " "), "Topic summary cannot be blank"),
        (lambda notebook: notebook.update_topic("t9", "open"), "Unknown topic `t9`"),
        (lambda notebook: notebook.edit("n1", " "), "Note text cannot be blank"),
        (lambda notebook: notebook.edit("n9", "Third"), "Unknown note `n9`"),
        (lambda notebook: notebook.delete([]), "unique note IDs"),
        (lambda notebook: notebook.delete(["n1", "n1"]), "unique note IDs"),
        (lambda notebook: notebook.delete(["n1", "n9"]), "Unknown note `n9`"),
        (lambda notebook: notebook.connect([]), "Provide one or more connections"),
        (lambda notebook: notebook.connect([CONNECTION, CONNECTION]), "must be unique"),
        (lambda notebook: notebook.connect([BLANK_LABEL_CONNECTION]), "Connection labels cannot be blank"),
        (lambda notebook: notebook.disconnect([]), "Provide one or more connections"),
        (lambda notebook: notebook.disconnect([CONNECTION, CONNECTION]), "must be unique"),
        (lambda notebook: notebook.disconnect([BLANK_LABEL_CONNECTION]), "Connection labels cannot be blank"),
    ],
    ids=[
        "add-nothing",
        "add-blank-text",
        "add-to-an-unknown-topic",
        "add-a-blank-topic",
        "add-a-taken-topic-name",
        "update-with-a-blank-summary",
        "update-an-unknown-topic",
        "edit-to-blank-text",
        "edit-an-unknown-note",
        "delete-nothing",
        "delete-a-repeated-note",
        "delete-an-unknown-note-mid-batch",
        "connect-nothing",
        "connect-a-repeated-connection",
        "connect-with-a-blank-label",
        "disconnect-nothing",
        "disconnect-a-repeated-connection",
        "disconnect-with-a-blank-label",
    ],
)
def test_rejects_an_invalid_request_without_changing_anything(tmp_path: Path, change: Change, reason: str) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    notebook.add(["First", "Second"], "t1")
    before = notebook.graph.model_copy(deep=True)

    with pytest.raises(ValueError, match=reason):
        change(notebook)

    assert notebook.graph == before
    assert Notebook(notebook.path).graph == before


def test_counts_only_the_connections_a_request_changes(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    notebook.add(["First", "Second", "Third"], "t1")
    stored = Connection(source_id="n1", target_id="n2", label="requires")
    new = Connection(source_id="n2", target_id="n3", label="requires")

    assert notebook.connect([stored]) == 1
    assert notebook.connect([stored]) == 0
    assert notebook.connect([stored, new]) == 1
    assert notebook.graph.connections == [stored, new]
    assert notebook.disconnect([Connection(source_id="n1", target_id="n3", label="supports")]) == 0
    assert notebook.disconnect([stored, new]) == len([stored, new])
    assert Notebook(notebook.path).graph.connections == []


@pytest.mark.parametrize(
    "label",
    ["contains", "Belongs To", "part of", "IN", "  is   part  of "],
    ids=["plain", "cased", "inverse", "bare", "spaced"],
)
def test_refuses_a_connection_that_only_repeats_where_a_note_already_sits(tmp_path: Path, label: str) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    delivery = notebook.add_topic("Delivery")
    notebook.add(["First"], delivery.id)

    with pytest.raises(ValueError, match="states nothing further"):
        notebook.connect([Connection(source_id=delivery.id, target_id="n1", label=label)])
    with pytest.raises(ValueError, match="states nothing further"):
        notebook.connect([Connection(source_id="n1", target_id=delivery.id, label=label)])

    assert notebook.graph.connections == []
    assert Notebook(notebook.path).graph.connections == []


def test_keeps_a_connection_that_says_more_than_where_a_note_sits(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    delivery = notebook.add_topic("Delivery")
    notebook.add(["First"], delivery.id)
    beyond = Connection(source_id=delivery.id, target_id="n1", label="is answered by")
    elsewhere = Connection(source_id="n1", target_id="t1", label="belongs to")

    assert notebook.connect([beyond, elsewhere]) == len([beyond, elsewhere])
    assert Notebook(notebook.path).graph.connections == [beyond, elsewhere]


# Loading a graph does not check for containment restatement; only `connect()` does. A file written by hand,
# or saved before this rule existed, must still load and its connection must still be removable.
def test_loads_a_notebook_already_holding_a_containment_connection(tmp_path: Path) -> None:
    path = tmp_path / "notebook.yaml"
    path.write_text(
        "topics:\n- id: t1\n  name: Overview\n  status: open\n  notes: {n1: First}\n"
        "connections: [t1 contains n1]\nnext_note_id: n2",
        encoding="utf-8",
    )
    notebook = Notebook(path)
    stored = Connection(source_id="t1", target_id="n1", label="contains")

    assert notebook.graph.connections == [stored]
    assert notebook.disconnect([stored]) == 1
    assert Notebook(path).graph.connections == []


def test_stores_a_stripped_topic_name_and_finds_it_by_id_or_name(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")

    topic = notebook.add_topic("  Delivery  ")

    assert topic.name == "Delivery"
    assert notebook.find_topic(topic.id) == topic
    assert notebook.find_topic(" DELIVERY ") == topic
    assert notebook.find_topic("Security") is None


def test_keeps_the_topic_summary_when_only_the_status_changes(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    topic = notebook.add_topic("Delivery")
    notebook.update_topic(topic.id, "done", "Delivery is fully defined.")

    updated = notebook.update_topic(topic.id, "trashed")

    assert (updated.status, updated.summary) == ("trashed", "Delivery is fully defined.")
    assert Notebook(notebook.path).find_topic(topic.id) == updated


def test_stores_a_copy_of_a_restored_graph(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    notebook.add(["First"], "t1")
    checkpoint = notebook.graph.model_copy(deep=True)

    notebook.restore(checkpoint)
    checkpoint.notes.append(Note(id="n9", topic_id="t1", text="Leaked"))
    checkpoint.topics.append(Topic(id="t9", name="Leaked", status="open"))

    assert [note.id for note in notebook.graph.notes] == ["n1"]
    assert [topic.id for topic in notebook.graph.topics] == ["t1"]


def test_returns_the_connections_between_the_notes_it_reaches(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    delivery = notebook.add_topic("Delivery")
    notebook.add(["First"], "t1")
    notebook.add(["Second"], delivery.id)
    notebook.add(["Third"], "t1")
    crossed = Connection(source_id="t1", target_id=delivery.id, label="relates to")
    entered = Connection(source_id="t1", target_id="n1", label="is answered by")
    followed = Connection(source_id="n1", target_id="n2", label="requires")
    unreached = Connection(source_id="n2", target_id="n3", label="requires")
    notebook.connect([crossed, entered, followed, unreached])

    notes, connections = notebook.read(ReadQuery(traverse_from=["t1"], direction="outgoing", depth=2))

    assert {note.id for note in notes} == {"n1", "n2"}
    assert connections == [crossed, entered, followed]


def test_ignores_an_empty_selector_list(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    notebook.add(["First"], "t1")

    assert [note.id for note in notebook.read(ReadQuery(ids=[]))[0]] == ["n1"]
    assert [note.id for note in notebook.read(ReadQuery(topic_ids=[]))[0]] == ["n1"]


def test_hides_a_trashed_note_requested_by_id(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml")
    topic = notebook.add_topic("Discarded idea")
    notebook.add(["Do not show this by default."], topic.id)
    notebook.update_topic(topic.id, "trashed")

    assert notebook.read(ReadQuery(ids=["n1"]))[0] == []
    assert [note.id for note in notebook.read(ReadQuery(ids=["n1"], topic_ids=[topic.id]))[0]] == ["n1"]
