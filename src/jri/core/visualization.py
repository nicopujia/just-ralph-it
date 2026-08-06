from pathlib import Path

from .notes import Graph
from .support import ISSUES_URL

DRAW_ERROR = f"The graph viewer loaded, but it could not draw the graph. Report it at {ISSUES_URL}."

# `jri view` is the one command that reads nothing but the user's own
# notebook, so it is the one command that must not need a network.
# The libraries that draw the graph ship with JRI and are written into
# the page, which costs 3.6 MB of minified JavaScript: mermaid's module
# build weighs 30 KB and then fetches 26 more chunks while it runs, so
# the bundled build is the only one that can be carried at all. The
# page is `.jri/visualization.html`, which Git ignores and every `jri
# view` rewrites, so the weight lands on disk and nowhere else.
LIBRARIES_DIR = Path(__file__).parent / "viewer"
LIBRARIES = ("mermaid.min.js", "svg-pan-zoom.min.js")

# The browser decodes HTML entities inside the `mermaid` block before
# mermaid parses it, so HTML escaping protects nothing: `&quot;` turns
# back into the `"` that ends a label. Mermaid's own entity codes
# survive that decoding and reach the parser as text.
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
# The template is mostly CSS and JavaScript, so it is full of braces and
# percentages that neither `%` nor `format` would leave alone. The slots
# are substituted literally instead.
DIAGRAM_SLOT = "<!-- diagram -->"
DRAW_ERROR_SLOT = "<!-- draw error -->"
LIBRARIES_SLOT = "<!-- libraries -->"
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
        <!-- libraries -->
        <script type="module">
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
    diagram = ["flowchart TD", "    classDef topic fill:#fff3cd,stroke:#856404,stroke-width:2px"]
    for topic in graph.topics:
        summary = topic.summary
        label = f"{_escape(topic.name)}<br/>[{topic.status}]"
        if summary:
            label += f"<br/>{_escape(summary)}"
        diagram.append(f'{INDENTATION}{topic.id}(["{label}"]):::topic')
    diagram.extend(f'{INDENTATION}{note.id}["{_escape(note.text)}"]' for note in graph.notes)
    connected_pairs = {(connection.source_id, connection.target_id) for connection in graph.connections}
    diagram.extend(
        f'{INDENTATION}{note.topic_id} -->|"contains"| {note.id}'
        for note in graph.notes
        if (note.topic_id, note.id) not in connected_pairs
    )
    diagram.extend(
        f'{INDENTATION}{connection.source_id} -->|"{_escape(connection.label)}"| {connection.target_id}'
        for connection in graph.connections
    )
    return (
        HTML
        .replace(DIAGRAM_SLOT, "\n".join(diagram))
        .replace(DRAW_ERROR_SLOT, DRAW_ERROR)
        # Last, so the substitutions above read a page of JRI's size
        # rather than one carrying megabytes of somebody else's.
        .replace(
            LIBRARIES_SLOT,
            "\n".join(f"<script>{(LIBRARIES_DIR / name).read_text(encoding='utf-8')}</script>" for name in LIBRARIES),
        )
    )


def _escape(value: str) -> str:
    return value.translate(ESCAPES).replace("\n", "<br/>")
