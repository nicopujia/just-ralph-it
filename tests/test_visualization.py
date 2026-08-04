from jri.core.notes import Connection, Graph, Note, Topic
from jri.core.visualization import render

GRAPH = Graph(
    topics=[
        Topic(id="t1", name="Project overview", status="open", summary="A tool for <everyone>"),
        Topic(id="t2", name="Delivery", status="done"),
    ],
    notes=[Note(id="n1", topic_id="t1", text="Deploy from <main>"), Note(id="n2", topic_id="t2", text="Ship weekly")],
    connections=[Connection(source_id="t2", target_id="n2", label="starts with")],
)


def test_draws_every_topic_with_its_status_and_summary() -> None:
    diagram = render(GRAPH)

    assert 't1(["Project overview<br/>[open]<br/>A tool for &lt;everyone&gt;"]):::topic' in diagram
    assert 't2(["Delivery<br/>[done]"]):::topic' in diagram


def test_draws_every_note_with_its_text_escaped() -> None:
    diagram = render(GRAPH)

    assert 'n1["Deploy from &lt;main&gt;"]' in diagram
    assert 'n2["Ship weekly"]' in diagram


def test_holds_a_note_to_its_topic_only_when_nothing_else_does() -> None:
    diagram = render(GRAPH)

    assert 't1 -->|"contains"| n1' in diagram
    assert 't2 -->|"contains"| n2' not in diagram
    assert 't2 -->|"starts with"| n2' in diagram


def test_wraps_the_diagram_in_a_standalone_page() -> None:
    diagram = render(Graph())

    assert diagram.startswith("<!doctype html>")
    assert '<pre class="mermaid">' in diagram
    assert "flowchart TD" in diagram
