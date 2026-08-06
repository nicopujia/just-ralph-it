import pytest

from jri.core.notes import Connection, Graph, Note, Topic
from jri.core.visualization import (
    DIAGRAM_SLOT,
    DRAW_ERROR,
    DRAW_ERROR_SLOT,
    HTML,
    LIBRARIES,
    LIBRARIES_DIR,
    LIBRARIES_SLOT,
    render,
)


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


def test_says_what_went_wrong_where_the_page_can_show_it() -> None:
    page = render(build_graph())

    assert DRAW_ERROR in page
    assert DIAGRAM_SLOT not in page
    assert DRAW_ERROR_SLOT not in page
    assert LIBRARIES_SLOT not in page


def test_carries_everything_the_page_runs_so_none_of_it_is_fetched() -> None:
    page = render(build_graph())

    # The rendered page quotes a URL to report a failure to, so the
    # host the page would have to reach is looked for in the template.
    assert "://" not in HTML
    for name in LIBRARIES:
        assert (LIBRARIES_DIR / name).read_text(encoding="utf-8") in page


# Carrying the libraries is half of it: the page's own script reaches
# each of them by the global name it defines, and a library left out of
# the page leaves that name undefined. The call then throws inside the
# `try` after mermaid has already drawn the graph, so the handler wipes
# the drawn graph and leaves the error string in its place. Each pair
# below is a call the script makes and the assignment that gives it
# something to call; a library upgrade that moves the assignment has to
# say here where the global comes from now.
@pytest.mark.parametrize(
    ("call", "definition"),
    [("await mermaid.run();", 'globalThis["mermaid"]'), ("window.svgPanZoom(", "svgPanZoom=")],
    ids=["mermaid", "svg-pan-zoom"],
)
def test_carries_a_definition_for_every_global_its_script_calls(call: str, definition: str) -> None:
    page = render(build_graph())

    assert call in page
    assert definition in page


# A page that carries the libraries has to carry them as text inside a
# `<script>`, and the element ends at the first `</script` the browser
# reads, wherever it comes from. Everything after that would be parsed
# as markup instead of run as code.
def test_carries_libraries_that_cannot_end_the_element_holding_them() -> None:
    for name in LIBRARIES:
        assert "</script" not in (LIBRARIES_DIR / name).read_text(encoding="utf-8")
