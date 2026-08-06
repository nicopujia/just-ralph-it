import tomllib
from pathlib import Path

import pytest

from jri.core.notes import Connection, Graph, Note, Topic
from jri.core.visualization import DRAW_ERROR, LOAD_ERROR, render

# Where the project declares the tracker it takes reports at, so a
# message offering one is read against the declaration and not against
# a second copy of the same string.
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


# Every label below is a sentence a user could write, paired with what
# mermaid has to receive for it to read back as written: a delimiter
# arriving as itself ends the label early and the page becomes a parse
# error instead of a graph. Only a browser settles whether these codes
# are the right ones, which is what `jri view` is for; what a test can
# settle is that a note's own text never reaches the parser raw.
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


# The colours below only exist together: mermaid draws its edges in
# #333 and its topic text in black, so the canvas they land on has to
# be the light one they were chosen for, whatever scheme the browser
# is following. Only a browser settles whether the page reads well;
# what a test can settle is that neither half of the pin is dropped.
def test_pins_the_page_to_the_scheme_the_graph_is_drawn_for() -> None:
    page = render(build_graph())

    assert "background: #fff;" in page
    assert "color-scheme: light;" in page
    assert 'theme: "default"' in page


def test_opens_the_graph_at_its_top_instead_of_its_middle() -> None:
    page = render(build_graph())

    assert "center: false," in page


# A viewer that cannot draw has one thing left to offer, and it is the
# address: the sentence around it can be reworded, but a message that
# names a failure and nowhere to take it puts the reader back where a
# first name with no channel behind it left them.
def test_names_the_tracker_the_project_declares_for_reports() -> None:
    tracker = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["urls"]["Issues"]

    assert tracker in render(build_graph())


def test_says_what_went_wrong_where_the_page_can_show_it() -> None:
    page = render(build_graph())

    assert LOAD_ERROR in page
    assert DRAW_ERROR in page
    assert "<!--" not in page


# The page's script reaches each library by the global name it defines,
# and a library the page never fetches leaves that name undefined.
# Dropping a name from the page is invisible to a test that reads the
# page for what it fetches alone, since a page that fetches nothing and
# calls nothing is consistent. Each pair below is a call the script
# makes and the URL that gives it something to call; a library moved to
# another host or version has to say here where the global comes from.
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
