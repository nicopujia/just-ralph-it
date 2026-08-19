import tomllib
from pathlib import Path

import pytest

from jri.core.notes import Connection, Graph, Note, Topic
from jri.core.visualization import DRAW_ERROR, LOAD_ERROR, render

# Read the tracker URL from the project declaration.
# Do not keep a second copy of the same URL.
# This prevents the URL values from becoming inconsistent.
PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def build_graph(*, name: str = "Delivery", text: str = "Runs in a terminal.", label: str = "supports") -> Graph:
    return Graph(
        topics=[Topic(id="t1", name=name, status="open", summary="How it ships")],
        notes=[Note(id="n1", topic_id="t1", text=text), Note(id="n2", topic_id="t1", text="Ships as a wheel.")],
        connections=[Connection(source_id="n1", target_id="n2", label=label)],
        next_note_id="n3",
    )


def read_diagram(page: str) -> str:
    return page.split('<pre class="mermaid">')[1].split("</pre>", maxsplit=1)[0]


# Mermaid reads the diagram type from the first line, and a topic box painted by a class needs that class
# declared. A page missing either shows a parse error instead of the graph.
def test_opens_the_diagram_with_its_type_and_the_topic_style() -> None:
    diagram = read_diagram(render(build_graph()))

    assert diagram.strip().startswith("flowchart TD\n")
    assert "classDef topic fill:#fff3cd,stroke:#856404,stroke-width:2px" in diagram
    assert "class t1 topic" in diagram


def test_draws_an_edge_between_the_notes_a_connection_names() -> None:
    diagram = read_diagram(render(build_graph()))

    assert 'n1 -->|"supports"| n2' in diagram


# A summary is optional, so its separator must come with it.
def test_draws_a_topic_that_has_no_summary_without_a_separator() -> None:
    graph = Graph(topics=[Topic(id="t1", name="Delivery", status="open")], next_note_id="n1")

    diagram = read_diagram(render(graph))

    assert 'subgraph t1["Delivery [open]"]' in diagram


# A note sits in the box of its topic and a topic sits in the box of the topic above it, so the drawing states
# where every note stands without an edge for it.
def test_draws_a_note_and_a_subtopic_inside_the_topic_that_holds_them() -> None:
    graph = Graph(
        topics=[
            Topic(id="t1", name="Delivery", status="open", summary="How it ships"),
            Topic(id="t2", parent_id="t1", name="Rollout", status="open", summary="How it reaches users"),
        ],
        notes=[Note(id="n1", topic_id="t1", text="Ships as a wheel."), Note(id="n2", topic_id="t2", text="By region.")],
        next_note_id="n3",
    )

    diagram = read_diagram(render(graph))

    assert (
        '                subgraph t1["Delivery [open] — How it ships"]\n'
        '                    n1["Ships as a wheel."]\n'
        '                    subgraph t2["Rollout [open] — How it reaches users"]\n'
        '                        n2["By region."]\n'
        "                    end\n"
        "                end"
    ) in diagram


# These labels are texts that a user can write.
# Each expected label is safe Mermaid input for that text.
# A raw delimiter can close a label early.
# Then Mermaid reports a parse error instead of a graph.
# Only a browser can verify that the encoded labels look correct.
# This test verifies that raw note text does not reach Mermaid.
@pytest.mark.parametrize(
    ("text", "label"),
    [
        ('Calls them "topics".', "Calls them #quot;topics#quot;."),
        ("Reads a | b as a table.", "Reads a #124; b as a table."),
        ("Indexes rows[0] first.", "Indexes rows#91;0#93; first."),
        ("Renders <b>bold</b> text.", "Renders #lt;b#gt;bold#lt;/b#gt; text."),
        ("Quotes `code` inline.", "Quotes #96;code#96; inline."),
        ("Tags issue #12 as done.", "Tags issue #35;12 as done."),
        ("Joins Q&A into one topic.", "Joins Q#amp;A into one topic."),
        ("Runs in a terminal.\nAnd in a browser.", "Runs in a terminal.<br/>And in a browser."),
    ],
    ids=["quote", "pipe", "brackets", "angles", "backtick", "hash", "ampersand", "newline"],
)
def test_draws_a_note_whose_text_holds_a_delimiter(text: str, label: str) -> None:
    diagram = read_diagram(render(build_graph(text=text)))

    assert f'n1["{label}"]' in diagram


def test_draws_a_connection_whose_label_holds_a_delimiter() -> None:
    diagram = read_diagram(render(build_graph(label='needs "review"')))

    assert 'n1 -->|"needs #quot;review#quot;"| n2' in diagram


def test_draws_a_topic_whose_name_holds_a_delimiter() -> None:
    diagram = read_diagram(render(build_graph(name="Delivery | Packaging")))

    assert 'subgraph t1["Delivery #124; Packaging [open] — How it ships"]' in diagram


# The page embeds CSS and JavaScript, and both use `%` and `{}`. A `%`-format or `str.format` substitution would
# read those characters as its own placeholders and corrupt the page.
def test_leaves_the_percentages_and_braces_of_the_page_alone() -> None:
    page = render(build_graph())

    assert "width: 100%;" in page
    assert "mermaid.initialize({" in page


# Mermaid draws 500 edges and 50,000 characters of diagram at the most, and a notebook passes both numbers while a
# browser still draws it. A diagram cannot raise either limit, so the page states them where Mermaid reads them.
def test_raises_the_sizes_mermaid_draws_up_to() -> None:
    page = render(build_graph())

    assert "maxEdges: 600," in page
    assert "maxTextSize: 100000," in page


# Above its limits Mermaid puts a red box in the place of the graph, or it stops with an error that the page
# reports as a JRI failure. JRI counts the connections first, so the page states the size instead.
def test_says_the_notebook_is_too_large_when_it_holds_more_connections_than_the_viewer_draws() -> None:
    graph = Graph(
        topics=[Topic(id="t1", name="Delivery", status="open")],
        notes=[Note(id=f"n{number}", topic_id="t1", text="A note.") for number in range(1, 603)],
        connections=[
            Connection(source_id=f"n{number}", target_id=f"n{number + 1}", label="supports") for number in range(1, 602)
        ],
        next_note_id="n603",
    )

    page = render(graph)

    assert "It has 601 connections and " in page
    assert "draws 600 connections and 100000 characters at the most" in page
    # The message stands alone in the body. A drawing that ran would report a failure over it.
    assert '<pre class="mermaid">' not in page
    assert 'if (document.querySelector(".mermaid"))' in page


def test_says_the_notebook_is_too_large_when_its_diagram_is_longer_than_the_viewer_draws() -> None:
    graph = Graph(
        topics=[Topic(id="t1", name="Delivery", status="open")],
        notes=[Note(id=f"n{number}", topic_id="t1", text="A note. " * 125) for number in range(1, 121)],
        next_note_id="n121",
    )

    page = render(graph)

    assert "characters of diagram, and the graph viewer draws" in page
    assert '<pre class="mermaid">' not in page


# These colors must remain together.
# Mermaid uses `#333` edges and black topic text.
# They require the light canvas that they were selected for.
# The browser color scheme must not change that canvas.
# This test verifies both required color settings.
def test_pins_the_page_to_the_scheme_the_graph_is_drawn_for() -> None:
    page = render(build_graph())

    assert "background: #fff;" in page
    assert "color-scheme: light;" in page
    assert 'theme: "default"' in page


# A notebook graph is wider than it is tall. Centring it would split the leftover viewport height into a band
# above and a band below; anchoring it at the top spends that height once, past the last note.
def test_opens_the_graph_at_its_top_instead_of_its_middle() -> None:
    page = render(build_graph())

    assert "center: false," in page


# A viewer that cannot draw can still provide an address.
# The message can use different words around that address.
# A failure message without a destination does not help the user.
# It has the same problem as a name without a contact channel.
def test_names_the_tracker_the_project_declares_for_reports() -> None:
    tracker = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["urls"]["Issues"]

    assert tracker in render(build_graph())


# The page has two failure paths and one place to write a message in.
# A viewer that could not fetch its libraries must read the necessary user action.
# It must not read a request to report a drawing failure that did not occur.
# The message can use different words around that action.
def test_says_what_went_wrong_where_the_page_can_show_it() -> None:
    page = render(build_graph())

    before_drawing, while_drawing = page.split("await mermaid.run();")

    assert LOAD_ERROR in before_drawing
    assert LOAD_ERROR not in while_drawing
    assert DRAW_ERROR in while_drawing
    assert DRAW_ERROR not in before_drawing
    assert "`jri view`" in before_drawing
    assert "<!--" not in page


# The page script uses each library global name.
# A library that the page does not load leaves that name undefined.
# A fetch-only test cannot detect a missing global call.
# A page with no fetches and no calls is still internally consistent.
# Each pair gives a script call and its library URL.
# Update this test when a library host or version changes.
# The URL must define the global name that the script uses.
@pytest.mark.parametrize(
    ("call", "source"),
    [
        ("await mermaid.run();", "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"),
        ("window.svgPanZoom(", "https://cdn.jsdelivr.net/npm/svg-pan-zoom@3.6.2/dist/svg-pan-zoom.min.js"),
    ],
    ids=["mermaid", "svg-pan-zoom"],
)
def test_fetches_a_source_for_every_global_its_script_calls(call: str, source: str) -> None:
    page = render(build_graph())

    assert call in page
    assert source in page
