from collections.abc import Iterator

from . import issues
from .notes import Graph, Topic

DRAW_ERROR = f"The graph viewer loaded, but it could not draw the graph. Report it at {issues.URL}."
# The page fetches its drawing libraries, and it does not hold them.
# JRI needs a network connection to the model provider, so a viewer that works offline alone has no value.
# A Mermaid build that holds everything adds 3.6 MB to the wheel and to each `jri view` page.
# State that the fetch failed, and state what the user must do.
LOAD_ERROR = (
    "The graph viewer could not load what it needs from the internet. Check your connection and run `jri view` again."
)
# Mermaid draws 500 edges and 50,000 characters of diagram at the most.
# A notebook passes both numbers while a browser still draws it quickly.
# Mermaid also refuses in two different ways. Above the character limit it puts a red box in the place of the
# graph. Above the edge limit it stops with an error, and the page reports that error as a JRI failure.
# Set the limits from the time that the drawing takes instead.
# Chrome drew 400 notes and 200 connections in 6 seconds, 600 notes and 600 connections in 31 seconds, and
# 800 notes and 800 connections in 68 seconds.
# The page shows no progress, so a user reads a longer wait as a stop.
# These two numbers are thus the largest notebook that a browser draws in about half a minute.
MAX_CONNECTIONS = 600
MAX_TEXT_SIZE = 100_000
# JRI counts the same two things before it writes the page.
# The user reads the size of the notebook, and not one of the two Mermaid refusals.
# Name all four numbers, because the two limits alone do not say which limit the notebook passed.
SIZE_ERROR = (
    "The notebook is too large to draw. It has {connections} connections and {characters} characters of diagram, and "
    "the graph viewer draws {max_connections} connections and {max_characters} characters at the most. "
    "Discard notes or connections in `jri chat`, then run `jri view` again."
)

# The browser decodes HTML entities in a `mermaid` block before Mermaid parses it.
# An HTML escape does not protect a label, because `&quot;` becomes the `"` that ends the label.
# A Mermaid entity code stays whole through that decoding, and it reaches the parser as text.
ESCAPES = str.maketrans({
    "#": "#35;",
    "&": "#amp;",
    '"': "#quot;",
    "<": "#lt;",
    ">": "#gt;",
    "`": "#96;",
    "[": "#91;",
    "]": "#93;",
    "|": "#124;",
})
INDENTATION = "    " * 3
# The template holds CSS and JavaScript braces and percent signs. `%` and `format` would change them.
# Replace these slots as plain text instead.
BODY_SLOT = "<!-- body -->"
DIAGRAM_SLOT = "<!-- diagram -->"
DRAW_ERROR_SLOT = "<!-- draw error -->"
LOAD_ERROR_SLOT = "<!-- load error -->"
MAX_CONNECTIONS_SLOT = "<!-- max connections -->"
MAX_TEXT_SIZE_SLOT = "<!-- max text size -->"
# The body holds the graph or it holds the message that says why it does not. The script draws only the first one.
DIAGRAM_BODY = """\
<pre class="mermaid">
<!-- diagram -->
        </pre>\
"""
HTML = """\
<!doctype html>
<html lang="en">
    <head>
        <meta charset="utf-8">
        <style>
            html, body, .mermaid, .mermaid svg {
                width: 100%;
                height: 100%;
                max-width: none !important;
                margin: 0;
            }

            /* The graph is drawn in mermaid's light palette and JRI's
               own amber topics, so the page pins the same appearance
               wherever it is opened: a browser following a dark scheme
               would otherwise paint the canvas black behind #333
               edges, and behind the black text an error is written in. */
            html {
                background: #fff;
                color-scheme: light;
            }

            body {
                overflow: hidden;
            }
        </style>
        <script src="https://cdn.jsdelivr.net/npm/svg-pan-zoom@3.6.2/dist/svg-pan-zoom.min.js"></script>
        <script type="module">
            // A body that holds a message instead of a graph needs no library and has nothing to draw.
            if (document.querySelector(".mermaid")) {
                let mermaid;
                try {
                    ({ default: mermaid } = await import(
                        "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"
                    ));
                    if (!window.svgPanZoom) {
                        throw new Error("svg-pan-zoom is missing");
                    }
                } catch {
                    document.body.textContent = "<!-- load error -->";
                }
                if (mermaid) {
                    try {
                        // Mermaid reads its two size limits here alone. A diagram cannot raise them itself.
                        mermaid.initialize({
                            startOnLoad: false,
                            theme: "default",
                            maxEdges: <!-- max connections -->,
                            maxTextSize: <!-- max text size -->,
                        });
                        await mermaid.run();
                        // The freshly inserted SVG has no layout yet. Sizing the
                        // pan and zoom now would measure it as 0x0 and scale the
                        // graph away to nothing.
                        await new Promise((paint) => requestAnimationFrame(paint));
                        // A notebook is wider than it is tall, so centring the
                        // fitted graph splits the leftover height into a band
                        // above it and a band below. Anchoring it at the top
                        // spends that height once, past the last note.
                        window.svgPanZoom(document.querySelector(".mermaid svg"), {
                            controlIconsEnabled: true,
                            center: false,
                        });
                    } catch {
                        document.body.textContent = "<!-- draw error -->";
                    }
                }
            }
        </script>
    </head>
    <body>
        <!-- body -->
    </body>
</html>\
"""


def render(graph: Graph) -> str:
    overview = graph.read_overview()
    lines = ["flowchart TD", f"{INDENTATION}classDef topic fill:#fff3cd,stroke:#856404,stroke-width:2px"]
    lines.extend(_draw_topic(overview, graph, graph.read_subtopics(), 1))
    # A note is inside the box of its topic, so that relation needs no edge.
    # Draw the connections after every box, because an edge inside a box puts both of its notes in that box.
    lines.extend(
        f'{INDENTATION}{connection.source_id} -->|"{_escape(connection.label)}"| {connection.target_id}'
        for connection in graph.connections
    )
    lines.append(f"{INDENTATION}class {','.join(topic.id for topic in graph.topics)} topic")
    # Mermaid measures the whole block, and it reads the block without the space around it.
    diagram = "\n".join(lines)
    body = (
        DIAGRAM_BODY.replace(DIAGRAM_SLOT, diagram)
        if len(graph.connections) <= MAX_CONNECTIONS and len(diagram) <= MAX_TEXT_SIZE
        else SIZE_ERROR.format(
            connections=len(graph.connections),
            characters=len(diagram),
            max_connections=MAX_CONNECTIONS,
            max_characters=MAX_TEXT_SIZE,
        )
    )
    return (
        HTML
        .replace(BODY_SLOT, body)
        .replace(LOAD_ERROR_SLOT, LOAD_ERROR)
        .replace(DRAW_ERROR_SLOT, DRAW_ERROR)
        .replace(MAX_CONNECTIONS_SLOT, str(MAX_CONNECTIONS))
        .replace(MAX_TEXT_SIZE_SLOT, str(MAX_TEXT_SIZE))
    )


def _draw_topic(topic: Topic, graph: Graph, subtopics: dict[str, list[Topic]], depth: int) -> Iterator[str]:
    margin = INDENTATION + "    " * depth
    # Mermaid gives the title of a subgraph one line of space, and it removes the lines below that line.
    # Keep the name, the status and the summary on that one line.
    label = f"{_escape(topic.name)} [{topic.status}]"
    if topic.summary:
        label += f" — {_escape(topic.summary)}"
    yield f'{margin}subgraph {topic.id}["{label}"]'
    yield from (f'{margin}    {note.id}["{_escape(note.text)}"]' for note in graph.notes if note.topic_id == topic.id)
    for child in subtopics.get(topic.id, []):
        yield from _draw_topic(child, graph, subtopics, depth + 1)
    yield f"{margin}end"


def _escape(value: str) -> str:
    return value.translate(ESCAPES).replace("\n", "<br/>")
