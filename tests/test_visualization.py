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


def test_draws_every_topic_note_and_connection() -> None:
    diagram = read_diagram(render(build_graph()))

    assert 't1(["Delivery<br/>[open]<br/>How it ships"]):::topic' in diagram
    assert 'n1["Runs in a terminal."]' in diagram
    assert 'n1 -->|"supports"| n2' in diagram


def test_hangs_a_note_off_its_topic_only_where_nothing_else_connects_them() -> None:
    graph = build_graph()
    graph.connections.append(Connection(source_id="t1", target_id="n1", label="asks about"))

    diagram = read_diagram(render(graph))

    assert 't1 -->|"contains"| n1' not in diagram
    assert 't1 -->|"contains"| n2' in diagram
    assert 't1 -->|"asks about"| n1' in diagram


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

    assert 't1(["Delivery #124; Packaging<br/>[open]<br/>How it ships"]):::topic' in diagram


def test_leaves_the_percentages_and_braces_of_the_page_alone() -> None:
    page = render(build_graph())

    assert "width: 100%;" in page
    assert 'mermaid.initialize({ startOnLoad: false, theme: "default" });' in page


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


def test_says_what_went_wrong_where_the_page_can_show_it() -> None:
    page = render(build_graph())

    assert LOAD_ERROR in page
    assert DRAW_ERROR in page
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
