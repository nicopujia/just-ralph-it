from collections.abc import Iterator

from . import issues
from .notes import OVERVIEW_TOPIC_ID, Graph, Topic

DRAW_ERROR = f"The graph viewer loaded, but it could not draw the graph. Report it at {issues.URL}."
# The page fetches its drawing libraries instead of carrying them. JRI requires a model-provider network connection.
# An offline viewer alone has no value.
# A self-contained Mermaid build adds 3.6 MB to the wheel and each `jri view` page.
# State the fetch failure and the required user action.
LOAD_ERROR = (
    "The graph viewer could not load what it needs from the internet. Check your connection and run `jri view` again."
)

# The browser decodes HTML entities in a `mermaid` block before Mermaid parses it.
# HTML escaping does not protect labels.
# `&quot;` becomes the `"` that ends a label. Mermaid entity codes survive decoding and reach the parser as text.
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
# The template contains CSS and JavaScript braces and percentages. `%` and `format` would change them.
# Substitute these slots literally instead.
DIAGRAM_SLOT = "<!-- diagram -->"
DRAW_ERROR_SLOT = "<!-- draw error -->"
LOAD_ERROR_SLOT = "<!-- load error -->"
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
                    mermaid.initialize({ startOnLoad: false, theme: "default" });
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
        </script>
    </head>
    <body>
        <pre class="mermaid">
            <!-- diagram -->
        </pre>
    </body>
</html>\
"""


def render(graph: Graph) -> str:
    overview = next(topic for topic in graph.topics if topic.id == OVERVIEW_TOPIC_ID)
    diagram = ["flowchart TD", f"{INDENTATION}classDef topic fill:#fff3cd,stroke:#856404,stroke-width:2px"]
    diagram.extend(_draw_topic(overview, graph, graph.read_subtopics(), 1))
    # A note sits inside the box of its topic, so containment needs no edge. Draw the connections after every box,
    # because an edge written inside one puts both of its notes in that box.
    diagram.extend(
        f'{INDENTATION}{connection.source_id} -->|"{_escape(connection.label)}"| {connection.target_id}'
        for connection in graph.connections
    )
    diagram.append(f"{INDENTATION}class {','.join(topic.id for topic in graph.topics)} topic")
    return (
        HTML
        .replace(DIAGRAM_SLOT, "\n".join(diagram))
        .replace(LOAD_ERROR_SLOT, LOAD_ERROR)
        .replace(DRAW_ERROR_SLOT, DRAW_ERROR)
    )


def _draw_topic(topic: Topic, graph: Graph, subtopics: dict[str, list[Topic]], depth: int) -> Iterator[str]:
    margin = INDENTATION + "    " * depth
    # Mermaid gives the title of a subgraph one line of space and cuts off the lines below it.
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
