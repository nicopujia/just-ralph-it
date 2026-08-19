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
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")

    assert [note.id for note in notebook.add(["one", "two", "three"], "t1")] == ["n1", "n2", "n3"]
    assert notebook.add_topic("Second topic", "t1", "What second topic covers.").id == "t2"
    assert [notebook.add_topic(name, "t1", f"What {name} covers.").id for name in ("Third", "Fourth")] == ["t3", "t4"]

    notebook = Notebook(notebook.path, "Acme")

    assert notebook.add(["four"], "t1")[0].id == "n4"
    assert notebook.add_topic("Fifth", "t1", "What fifth covers.").id == "t5"


# JRI only appends a topic, so a gap between topic IDs can come from a hand-edited file. A count of the topics
# would allocate an ID that a later topic already holds.
def test_allocates_a_topic_id_after_the_highest_one_in_the_file(tmp_path: Path) -> None:
    path = tmp_path / "notebook.yaml"
    path.write_text(
        "id: t1\nname: Overview\nstatus: open\nnotes: {n1: First}\n"
        "topics:\n- id: t3\n  name: Delivery\n  status: open\n  notes: {}\n"
        "connections: []\nnext_note_id: n2",
        encoding="utf-8",
    )

    assert Notebook(path, "Acme").add_topic("Security", "t1", "What security covers.").id == "t4"


# Each shape below breaks one rule, and each rule has wording of its own. A shape refused for another rule than the
# one it breaks says the rule it breaks is gone, so every case names the wording that must refuse it.
@pytest.mark.parametrize(
    ("data", "wording"),
    [
        (
            {
                **VALID_GRAPH,
                "topics": [
                    {"id": "t1", "name": "Overview", "status": "open"},
                    {"id": "tx", "name": "Delivery", "status": "open"},
                ],
            },
            "String should match pattern",
        ),
        (
            {
                **VALID_GRAPH,
                "notes": [
                    {"id": "n1", "topic_id": "t1", "text": "First"},
                    {"id": "n1", "topic_id": "t1", "text": "Second"},
                ],
            },
            "Topic and note IDs must be unique",
        ),
        (
            {
                **VALID_GRAPH,
                "topics": [
                    {"id": "t1", "name": "Overview", "status": "open"},
                    {"id": "t2", "name": " overview ", "status": "open"},
                ],
            },
            "Topic names must be unique",
        ),
        (
            {
                **VALID_GRAPH,
                "topics": [{"id": "t2", "name": "Overview", "status": "open"}],
                "notes": [{"id": "n1", "topic_id": "t2", "text": "A requirement"}],
            },
            "The overview topic `t1` must exist",
        ),
        (
            {**VALID_GRAPH, "notes": [{"id": "n1", "topic_id": "t2", "text": "A requirement"}]},
            "Every note must reference an existing topic",
        ),
        (
            {**VALID_GRAPH, "connections": [{"source_id": "n1", "target_id": "n2", "label": "requires"}]},
            "Connection endpoints must reference existing notes",
        ),
        (
            {
                **VALID_GRAPH,
                "connections": [
                    {"source_id": "n1", "target_id": "n1", "label": "supports"},
                    {"source_id": "n1", "target_id": "n1", "label": "supports"},
                ],
            },
            "Connections must be unique",
        ),
        (
            {
                **VALID_GRAPH,
                "topics": [
                    {"id": "t1", "name": "Overview", "status": "open"},
                    {"id": "t2", "name": "Delivery", "status": "open"},
                ],
            },
            "Only the overview topic `t1` stands without a parent topic",
        ),
        (
            {
                **VALID_GRAPH,
                "topics": [
                    {"id": "t1", "name": "Overview", "status": "open"},
                    {"id": "t2", "parent_id": "t9", "name": "Delivery", "status": "open"},
                ],
            },
            "Every topic must reference an existing parent topic",
        ),
        (
            {
                **VALID_GRAPH,
                "topics": [
                    {"id": "t1", "name": "Overview", "status": "open"},
                    {"id": "t2", "parent_id": "t3", "name": "Delivery", "status": "open"},
                    {"id": "t3", "parent_id": "t2", "name": "Rollout", "status": "open"},
                ],
            },
            "cannot stand inside itself",
        ),
        (
            {
                **VALID_GRAPH,
                "topics": [
                    {"id": "t1", "name": "Overview", "status": "open"},
                    {"id": "t2", "parent_id": "t1", "name": "Delivery", "status": "open"},
                    {"id": "t3", "parent_id": "t2", "name": "Rollout", "status": "open"},
                    {"id": "t4", "parent_id": "t3", "name": "Regions", "status": "open"},
                ],
            },
            "Topics nest 3 levels deep at most",
        ),
        ({**VALID_GRAPH, "topics": [{"id": "t1", "name": " ", "status": "open"}]}, "Graph content cannot be blank"),
        ({**VALID_GRAPH, "next_note_id": "n1"}, "Note IDs must come before `n1`"),
    ],
    ids=[
        "malformed-id",
        "duplicate-id",
        "duplicate-topic-name",
        "missing-overview",
        "missing-note-topic",
        "dangling-connection",
        "duplicate-connection",
        "second-root",
        "unknown-parent",
        "topic-inside-itself",
        "nested-too-deep",
        "blank-content",
        "taken-next-id",
    ],
)
def test_rejects_invalid_graph_data(data: dict[str, Any], wording: str) -> None:
    with pytest.raises(ValidationError, match=wording):
        Graph.model_validate(data)


def test_rejects_an_invalid_connection_batch_without_changing_anything(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    notebook.add(["First", "Second"], "t1")
    before = notebook.graph.model_copy(deep=True)

    with pytest.raises(ValueError, match="n99"):
        notebook.connect([
            Connection(source_id="n1", target_id="n2", label="requires"),
            Connection(source_id="n1", target_id="n99", label="requires"),
        ])

    assert notebook.graph == before
    assert Notebook(notebook.path, "Acme").graph == before


def test_never_reuses_the_id_of_a_deleted_note(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    notebook.add(["First", "Second", "Third"], "t1")

    notebook.delete(["n3"])

    assert notebook.add(["Fourth"], "t1")[0].id == "n4"
    assert Notebook(notebook.path, "Acme").add(["Fifth"], "t1")[0].id == "n5"


def test_keeps_the_next_note_id_when_restoring_an_earlier_graph(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    notebook.add(["First"], "t1")
    checkpoint = notebook.graph.model_copy(deep=True)
    notebook.add(["Second"], "t1")

    notebook.restore(checkpoint)

    assert [note.id for note in notebook.graph.notes] == ["n1"]
    assert notebook.add(["Third"], "t1")[0].id == "n3"


def test_removes_the_connections_of_a_deleted_note(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    notebook.add(["First", "Second", "Third"], "t1")
    notebook.connect([
        Connection(source_id="n1", target_id="n2", label="requires"),
        Connection(source_id="n2", target_id="n3", label="requires"),
        Connection(source_id="n1", target_id="n3", label="supports"),
    ])

    notebook.delete(["n2"])
    graph = Notebook(notebook.path, "Acme").graph

    assert {note.id for note in graph.notes} == {"n1", "n3"}
    assert {(item.source_id, item.target_id, item.label) for item in graph.connections} == {("n1", "n3", "supports")}


def test_restores_notebook_changes_after_restart(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    topic = notebook.add_topic("Delivery", "t1", "What delivery covers.")
    first, second = notebook.add(["Deploy manually.", "Use the main branch 🚀."], topic.id)
    connection = Connection(source_id=first.id, target_id=second.id, label="requires")
    notebook.connect([connection])

    notebook.edit(first.id, "Deploy automatically.")
    notebook.update_topic(topic.id, "done", "Delivery is fully defined.")

    notebook = Notebook(notebook.path, "Acme")
    restored_topic = notebook.find_topic(topic.id)
    assert restored_topic is not None
    assert (restored_topic.status, restored_topic.summary) == ("done", "Delivery is fully defined.")
    assert [(note.id, note.text) for note in notebook.graph.notes] == [
        (first.id, "Deploy automatically."),
        (second.id, "Use the main branch 🚀."),
    ]
    assert notebook.graph.connections == [connection]

    assert notebook.disconnect([connection]) == 1
    assert Notebook(notebook.path, "Acme").graph.connections == []


# The file stores each note under its topic in the order the graph holds them, so a load that sorts the IDs as
# text puts `n10` before `n2` and the next save rewrites the user's notebook in that order.
def test_keeps_notes_in_the_order_they_were_added_after_a_restart(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    added = [note.id for note in notebook.add([f"Note {index}" for index in range(12)], "t1")]

    notebook = Notebook(notebook.path, "Acme")
    notebook.add(["One more"], "t1")

    assert [note.id for note in notebook.graph.notes] == [*added, "n13"]
    assert list(safe_load(notebook.path.read_text(encoding="utf-8"))["notes"]) == [*added, "n13"]


def test_stores_notes_in_a_compact_schema(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    delivery = notebook.add_topic("Delivery", "t1", "What delivery covers.")
    first = notebook.add(["First"], "t1")[0]
    second = notebook.add(["Second 🚀"], delivery.id)[0]
    third = notebook.add(["Third"], "t1")[0]
    notebook.update_topic(delivery.id, "done", "Delivery is fully defined.")
    notebook.connect([
        Connection(source_id=first.id, target_id=second.id, label="supports"),
        Connection(source_id=first.id, target_id=third.id, label="strongly supports"),
    ])

    assert notebook.path.read_text(encoding="utf-8") == (
        "id: t1\n"
        "name: Acme\n"
        "status: open\n"
        "notes:\n"
        "  n1: First\n"
        "  n3: Third\n"
        "topics:\n"
        "- id: t2\n"
        "  name: Delivery\n"
        "  summary: Delivery is fully defined.\n"
        "  status: done\n"
        "  notes:\n"
        "    n2: Second 🚀\n"
        "connections:\n"
        "- n1 supports n2\n"
        "- n1 strongly supports n3\n"
        "next_note_id: n4\n"
    )
    assert Notebook(notebook.path, "Acme").graph == notebook.graph


def test_renders_the_active_branch_and_names_every_other_topic(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    delivery = notebook.add_topic("Delivery", "t1", "How it ships.")
    rollout = notebook.add_topic("Rollout", delivery.id, "How it reaches users.")
    security = notebook.add_topic("Security", "t1", "How it is secured.")
    discarded = notebook.add_topic("Discarded", "t1", "What was dropped.")
    notebook.add_topic("Dropped detail", discarded.id, "A detail of what was dropped.")
    notebook.add(["Overview"], "t1")
    notebook.add(["Ship weekly."], delivery.id)
    notebook.add(["Roll out by region."], rollout.id)
    notebook.add(["Encrypt credentials."], security.id)
    notebook.add(["Do not build this."], discarded.id)
    notebook.connect([
        Connection(source_id="n1", target_id="n3", label="supports"),
        Connection(source_id="n4", target_id="n3", label="limits"),
        Connection(source_id="n4", target_id="n5", label="supersedes"),
    ])
    notebook.trash([discarded.id])

    assert safe_load(notebook.render(rollout.id)) == {
        "id": "t1",
        "name": "Acme",
        "status": "open",
        "notes": {"n1": "Overview"},
        "topics": [
            {
                "id": "t2",
                "name": "Delivery",
                "summary": "How it ships.",
                "status": "open",
                "notes": {"n2": "Ship weekly."},
                "topics": [
                    {
                        "id": "t3",
                        "name": "Rollout",
                        "summary": "How it reaches users.",
                        "status": "open",
                        "notes": {"n3": "Roll out by region."},
                    }
                ],
            },
            {"id": "t4", "name": "Security", "summary": "How it is secured.", "status": "open"},
        ],
        "connections": ["n1 supports n3", "n4 limits n3"],
    }


def test_excludes_a_trashed_topic_and_everything_under_it_from_a_document(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    discarded = notebook.add_topic("Discarded", "t1", "What was dropped.")
    detail = notebook.add_topic("Dropped detail", discarded.id, "A detail of what was dropped.")
    notebook.add(["Overview"], "t1")
    notebook.add(["Do not build this."], discarded.id)
    notebook.add(["Nor this."], detail.id)
    notebook.connect([Connection(source_id="n1", target_id="n2", label="supersedes")])
    notebook.trash([discarded.id])

    assert safe_load(Notebook.exclude_trashed(notebook.path.read_bytes())) == {
        "id": "t1",
        "name": "Acme",
        "status": "open",
        "notes": {"n1": "Overview"},
        "connections": [],
        "next_note_id": "n4",
    }


def test_leaves_a_document_holding_no_trashed_topic_as_it_stands(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    notebook.add(["Overview"], "t1")
    notebook.update_topic(notebook.add_topic("Delivery", "t1", "What delivery covers.").id, "done", "Settled.")

    assert Notebook.exclude_trashed(notebook.path.read_bytes()) == notebook.path.read_text()


def test_reads_a_notebook_never_written_as_an_empty_document() -> None:
    assert not Notebook.exclude_trashed(b"")


def test_reports_a_notebook_document_that_cannot_be_read() -> None:
    with pytest.raises(PersistenceError, match="cannot be read"):
        Notebook.exclude_trashed(b"nonsense")


# A hand-edited file can trash the overview topic. Every topic then reads as trashed, and what stays is no graph.
def test_reports_a_document_that_holds_nothing_once_its_trashed_topics_go() -> None:
    document = b"id: t1\nname: Acme\nstatus: trashed\nnotes: {n1: First}\nconnections: []\nnext_note_id: n2\n"

    with pytest.raises(PersistenceError, match="cannot be read"):
        Notebook.exclude_trashed(document)


def test_renders_an_empty_overview(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")

    assert safe_load(notebook.render("t1")) == {
        "id": "t1",
        "name": "Acme",
        "status": "open",
        "notes": {},
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
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    notebook.add(["First", "Second", "Third"], "t1")
    notebook.connect([
        Connection(source_id="n1", target_id="n2", label="requires"),
        Connection(source_id="n2", target_id="n3", label="requires"),
        Connection(source_id="n3", target_id="n1", label="closes"),
    ])

    notes, _ = notebook.read(query)

    assert {note.id for note in notes} == expected_ids


def test_ranks_fuzzy_matches_and_caps_the_result(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    notebook.add([*(f"Unrelated note {index}" for index in range(12)), "Deploy from the main branch."], "t1")

    notes, _ = notebook.read(ReadQuery(text="MAIN BRANCH"))

    assert notes[0].text == "Deploy from the main branch."
    assert len(notes) == MAX_SEARCH_RESULTS


def test_searches_only_visible_topics(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    topic = notebook.add_topic("Discarded idea", "t1", "What discarded idea covers.")
    notebook.add(["Deploy from the main branch."], topic.id)
    notebook.trash([topic.id])

    assert notebook.read(ReadQuery(text="main branch"))[0] == []
    assert [note.text for note in notebook.read(ReadQuery(text="main branch", topic_ids=[topic.id]))[0]] == [
        "Deploy from the main branch."
    ]


@pytest.mark.parametrize(
    ("query", "wording"),
    [
        ({"text": "  "}, "Search query cannot be blank"),
        ({"depth": 0}, "Traversal depth must be at least 1"),
        ({"traverse_from": ["n1"], "depth": -1}, "Traversal depth must be at least 1"),
    ],
    ids=["blank-text", "zero", "under"],
)
def test_rejects_invalid_search_selectors(query: dict[str, Any], wording: str) -> None:
    with pytest.raises(ValidationError, match=wording):
        ReadQuery(**query)


def test_rejects_unknown_selectors(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    notebook.add(["First"], "t1")

    with pytest.raises(ValueError, match="t99"):
        notebook.read(ReadQuery(topic_ids=["t99"]))
    with pytest.raises(ValueError, match="n99"):
        notebook.read(ReadQuery(ids=["n99"]))
    with pytest.raises(ValueError, match="Unknown note `n99`"):
        notebook.read(ReadQuery(traverse_from=["n99"]))


def test_hides_trashed_topics_unless_selected(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    topic = notebook.add_topic("Discarded idea", "t1", "What discarded idea covers.")
    notebook.add(["Do not show this by default."], topic.id)
    notebook.trash([topic.id])

    visible, _ = notebook.read(ReadQuery())
    selected, _ = notebook.read(ReadQuery(topic_ids=[topic.id]))

    assert visible == []
    assert [note.id for note in selected] == ["n1"]


@pytest.mark.parametrize(
    "contents",
    [
        ": invalid yaml",
        "a bare string",
        "connections: []\nnext_note_id: n1",
        "id: t1\nname: Overview\nstatus: open\nnotes: []\nconnections: []\nnext_note_id: n1",
        "id: t1\nname: Overview\nstatus: open\nnotes: {n1: First}\nconnections: [n1 malformed]\nnext_note_id: n2",
        (
            "id: t1\nname: Overview\nstatus: open\nnotes: {n1: First}\n"
            "connections: [n1 supports n1, n1 supports n1]\nnext_note_id: n2"
        ),
        "id: t1\nname: Overview\nstatus: open\nnotes: {n1: First}\nconnections: [n1 supports n2]\nnext_note_id: n2",
        "id: t1\nname: Overview\nstatus: open\nnotes: {n1: First}\nconnections: []",
        "id: t1\nname: Overview\nstatus: open\nnotes: {n1: First}\nconnections: []\nnext_note_id: n1",
    ],
    ids=[
        "malformed-yaml",
        "not-a-mapping",
        "no-root",
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
        Notebook(path, "Acme")


def test_reports_a_notebook_that_cannot_be_written(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    notebook.path.unlink()
    notebook.path.mkdir()
    (notebook.path / "blocker").write_text("taken")

    with pytest.raises(PersistenceError, match="Could not save the notebook file"):
        notebook.add(["A requirement"], "t1")

    assert sorted(path.name for path in tmp_path.iterdir()) == ["notebook.yaml"]


def test_keeps_the_last_saved_graph_in_memory_when_writing_fails(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    notebook.add(["First"], "t1")
    before = notebook.graph.model_copy(deep=True)
    notebook.path.unlink()
    notebook.path.mkdir()
    (notebook.path / "blocker").write_text("taken")

    with pytest.raises(PersistenceError, match="Could not save the notebook file"):
        notebook.add(["Second"], "t1")

    assert notebook.graph == before


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda notebook: notebook.add([], "t1"), "non-blank note texts"),
        (lambda notebook: notebook.add([" "], "t1"), "non-blank note texts"),
        (lambda notebook: notebook.add(["Third"], "t9"), "Unknown topic `t9`"),
        (lambda notebook: notebook.add_topic(" ", "t1", "What   covers."), "Topic name cannot be blank"),
        (lambda notebook: notebook.add_topic(" acme ", "t1", "What acme covers."), "already exists"),
        (lambda notebook: notebook.update_topic("t1", "open", " "), "Topic summary cannot be blank"),
        (lambda notebook: notebook.update_topic("t9", "open"), "Unknown topic `t9`"),
        (lambda notebook: notebook.update_topic("t1", name=" "), "Topic name cannot be blank"),
        (lambda notebook: notebook.add_topic("Delivery", "t1", " "), "Topic summary cannot be blank"),
        (lambda notebook: notebook.move([], "t1"), "unique note IDs"),
        (lambda notebook: notebook.move(["n1", "n1"], "t1"), "unique note IDs"),
        (lambda notebook: notebook.move(["n1"], "t9"), "Unknown topic `t9`"),
        (lambda notebook: notebook.edit("n1", " "), "Note text cannot be blank"),
        (lambda notebook: notebook.edit("n9", "Third"), "Unknown note `n9`"),
        (lambda notebook: notebook.delete([]), "unique note IDs"),
        (lambda notebook: notebook.delete(["n1", "n1"]), "unique note IDs"),
        (lambda notebook: notebook.delete(["n1", "n9"]), "Unknown note `n9`"),
        (lambda notebook: notebook.delete(["t1"]), "Unknown note `t1`"),
        (lambda notebook: notebook.trash([]), "unique topic IDs"),
        (lambda notebook: notebook.trash(["t1", "t1"]), "unique topic IDs"),
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
        "update-to-a-blank-name",
        "add-a-topic-without-a-summary",
        "move-nothing",
        "move-a-repeated-note",
        "move-to-an-unknown-topic",
        "edit-to-blank-text",
        "edit-an-unknown-note",
        "delete-nothing",
        "delete-a-repeated-note",
        "delete-an-unknown-note-mid-batch",
        "delete-a-topic",
        "trash-nothing",
        "trash-a-repeated-topic",
        "connect-nothing",
        "connect-a-repeated-connection",
        "connect-with-a-blank-label",
        "disconnect-nothing",
        "disconnect-a-repeated-connection",
        "disconnect-with-a-blank-label",
    ],
)
def test_rejects_an_invalid_request_without_changing_anything(tmp_path: Path, change: Change, reason: str) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    notebook.add(["First", "Second"], "t1")
    before = notebook.graph.model_copy(deep=True)

    with pytest.raises(ValueError, match=reason):
        change(notebook)

    assert notebook.graph == before
    assert Notebook(notebook.path, "Acme").graph == before


def test_counts_only_the_connections_a_request_changes(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    notebook.add(["First", "Second", "Third"], "t1")
    stored = Connection(source_id="n1", target_id="n2", label="requires")
    new = Connection(source_id="n2", target_id="n3", label="requires")

    assert notebook.connect([stored]) == 1
    assert notebook.connect([stored]) == 0
    assert notebook.connect([stored, new]) == 1
    assert notebook.graph.connections == [stored, new]
    assert notebook.disconnect([Connection(source_id="n1", target_id="n3", label="supports")]) == 0
    assert notebook.disconnect([stored, new]) == len([stored, new])
    assert Notebook(notebook.path, "Acme").graph.connections == []


def test_stores_a_stripped_topic_name_and_finds_it_by_id_or_name(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")

    topic = notebook.add_topic("  Delivery  ", "t1", "What   delivery   covers.")

    assert topic.name == "Delivery"
    assert notebook.find_topic(topic.id) == topic
    assert notebook.find_topic(" DELIVERY ") == topic
    assert notebook.find_topic("Security") is None


def test_keeps_the_topic_summary_when_only_the_status_changes(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    topic = notebook.add_topic("Delivery", "t1", "What delivery covers.")
    notebook.update_topic(topic.id, "done", "Delivery is fully defined.")

    assert notebook.trash([topic.id]) == [topic.id]

    restored = Notebook(notebook.path, "Acme").find_topic(topic.id)
    assert restored is not None
    assert (restored.status, restored.summary) == ("trashed", "Delivery is fully defined.")


def test_stores_a_copy_of_a_restored_graph(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    notebook.add(["First"], "t1")
    checkpoint = notebook.graph.model_copy(deep=True)

    notebook.restore(checkpoint)
    checkpoint.notes.append(Note(id="n9", topic_id="t1", text="Leaked"))
    checkpoint.topics.append(Topic(id="t9", name="Leaked", status="open"))

    assert [note.id for note in notebook.graph.notes] == ["n1"]
    assert [topic.id for topic in notebook.graph.topics] == ["t1"]


def test_returns_the_connections_between_the_notes_it_reaches(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    delivery = notebook.add_topic("Delivery", "t1", "What delivery covers.")
    notebook.add(["First"], "t1")
    notebook.add(["Second"], delivery.id)
    notebook.add(["Third"], "t1")
    followed = Connection(source_id="n1", target_id="n2", label="requires")
    unreached = Connection(source_id="n2", target_id="n3", label="requires")
    notebook.connect([followed, unreached])

    notes, connections = notebook.read(ReadQuery(traverse_from=["n1"], direction="outgoing", depth=1))

    assert {note.id for note in notes} == {"n1", "n2"}
    assert connections == [followed]


def test_ignores_an_empty_selector_list(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    notebook.add(["First"], "t1")

    assert [note.id for note in notebook.read(ReadQuery(ids=[]))[0]] == ["n1"]
    assert [note.id for note in notebook.read(ReadQuery(topic_ids=[]))[0]] == ["n1"]


def test_hides_a_note_under_a_trashed_topic_requested_by_id(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    topic = notebook.add_topic("Discarded idea", "t1", "What discarded idea covers.")
    notebook.add(["Do not show this by default."], topic.id)
    notebook.trash([topic.id])

    assert notebook.read(ReadQuery(ids=["n1"]))[0] == []
    assert [note.id for note in notebook.read(ReadQuery(ids=["n1"], topic_ids=[topic.id]))[0]] == ["n1"]


def test_moves_notes_to_another_topic_keeping_their_connections(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    rollout = notebook.add_topic("Rollout", "t1", "How it reaches users.")
    notebook.add(["Ship weekly.", "By region.", "Overview"], "t1")
    kept = Connection(source_id="n1", target_id="n2", label="requires")
    notebook.connect([kept])

    assert notebook.move(["n1", "n2"], rollout.id) == ["n1", "n2"]

    graph = Notebook(notebook.path, "Acme").graph
    assert [(note.id, note.topic_id) for note in graph.notes] == [("n1", "t2"), ("n2", "t2"), ("n3", "t1")]
    assert graph.connections == [kept]


def test_refuses_to_move_notes_into_a_topic_under_a_trashed_one(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    discarded = notebook.add_topic("Discarded", "t1", "What was dropped.")
    detail = notebook.add_topic("Dropped detail", discarded.id, "A detail of what was dropped.")
    notebook.add(["Overview"], "t1")
    notebook.trash([discarded.id])

    with pytest.raises(ValueError, match="is trashed"):
        notebook.move(["n1"], detail.id)

    assert Notebook(notebook.path, "Acme").graph.notes[0].topic_id == "t1"


def test_reads_a_topic_with_everything_under_it(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    delivery = notebook.add_topic("Delivery", "t1", "How it ships.")
    rollout = notebook.add_topic("Rollout", delivery.id, "How it reaches users.")
    notebook.add(["Overview"], "t1")
    notebook.add(["Ship weekly."], delivery.id)
    notebook.add(["By region."], rollout.id)

    notes, _ = notebook.read(ReadQuery(topic_ids=[delivery.id]))

    assert [note.id for note in notes] == ["n2", "n3"]


def test_hides_a_topic_under_a_trashed_one_and_gives_it_back_with_it(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    delivery = notebook.add_topic("Delivery", "t1", "How it ships.")
    rollout = notebook.add_topic("Rollout", delivery.id, "How it reaches users.")
    dropped = notebook.add_topic("Dropped", delivery.id, "What was dropped.")
    notebook.add(["Ship weekly."], delivery.id)
    notebook.add(["By region."], rollout.id)
    notebook.add(["Not this."], dropped.id)
    notebook.trash([dropped.id])
    notebook.trash([delivery.id])

    assert notebook.read(ReadQuery())[0] == []

    notebook.update_topic(delivery.id, "open")

    # The topic discarded on its own kept its own status, so restoring the one above it leaves that one discarded.
    assert [note.id for note in notebook.read(ReadQuery())[0]] == ["n1", "n2"]


def test_renames_a_topic_and_moves_it_under_another(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    delivery = notebook.add_topic("Delivery", "t1", "How it ships.")
    rollout = notebook.add_topic("Rollout", "t1", "How it reaches users.")

    updated = notebook.update_topic(rollout.id, name=" Regional rollout ", parent_id=delivery.id)

    assert (updated.name, updated.parent_id) == ("Regional rollout", delivery.id)
    assert Notebook(notebook.path, "Acme").find_topic("regional ROLLOUT") == updated


def test_refuses_to_take_the_name_of_another_topic(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    notebook.add_topic("Delivery", "t1", "How it ships.")
    rollout = notebook.add_topic("Rollout", "t1", "How it reaches users.")

    assert notebook.update_topic(rollout.id, name="Rollout").name == "Rollout"
    with pytest.raises(ValueError, match="already exists"):
        notebook.update_topic(rollout.id, name=" delivery ")


def test_refuses_to_stand_the_overview_topic_under_another(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    delivery = notebook.add_topic("Delivery", "t1", "How it ships.")

    with pytest.raises(ValueError, match="cannot stand under another topic"):
        notebook.update_topic("t1", parent_id=delivery.id)

    assert Notebook(notebook.path, "Acme").initial_topic.parent_id is None


def test_keeps_the_notes_of_a_trashed_topic_so_a_restore_gives_them_back(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    dropped = notebook.add_topic("Dropped", "t1", "What was dropped.")
    notebook.add(["Overview"], "t1")
    notebook.add(["Keep this until it is restored."], dropped.id)
    notebook.connect([Connection(source_id="n1", target_id="n2", label="requires")])

    assert notebook.trash([dropped.id]) == [dropped.id]

    graph = Notebook(notebook.path, "Acme").graph
    assert [note.id for note in graph.notes] == ["n1", "n2"]
    assert [(item.source_id, item.target_id) for item in graph.connections] == [("n1", "n2")]

    notebook.update_topic(dropped.id, "open")

    assert [note.id for note in notebook.read(ReadQuery())[0]] == ["n1", "n2"]


def test_refuses_to_stand_a_topic_under_a_trashed_one(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    discarded = notebook.add_topic("Discarded", "t1", "What was dropped.")
    live = notebook.add_topic("Live", "t1", "Still wanted.")
    notebook.trash([discarded.id])

    with pytest.raises(ValueError, match="is trashed"):
        notebook.update_topic(live.id, parent_id=discarded.id)

    reopened = Notebook(notebook.path, "Acme").find_topic(live.id)
    assert reopened is not None
    assert reopened.parent_id == "t1"


# A pinned connection can name a note the document does not show, because a topic keeps its edges to the rest of the
# project. It must not name a note of a topic that the document leaves out altogether.
def test_renders_nothing_of_a_topic_under_a_trashed_one(tmp_path: Path) -> None:
    notebook = Notebook(tmp_path / "notebook.yaml", "Acme")
    discarded = notebook.add_topic("Discarded", "t1", "What was dropped.")
    under = notebook.add_topic("Under", discarded.id, "It stands under the discarded topic.")
    notebook.add(["First.", "Second."], under.id)
    notebook.connect([Connection(source_id="n1", target_id="n2", label="requires")])
    notebook.trash([discarded.id])

    assert safe_load(notebook.render(under.id)) == {
        "id": "t1",
        "name": "Acme",
        "status": "open",
        "notes": {},
        "connections": [],
    }
