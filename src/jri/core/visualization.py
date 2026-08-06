from .notes import Graph
from .support import ISSUES_URL

DRAW_ERROR = f"The graph viewer loaded, but it could not draw the graph. Report it at {ISSUES_URL}."
# The page fetches what draws it rather than carrying it. Nothing JRI
# does works offline -- every interview turn is a call to a model
# provider -- so a viewer that survives a lost network survives alone,
# and the self-contained mermaid build that would buy it weighs 3.6 MB
# in the wheel and again in every page `jri view` writes. What the
# fetch costs is stated instead: a failure to load says so, and says
# what to do about it.
LOAD_ERROR = (
    "The graph viewer could not load what it needs from the internet. Check your connection and run `jri view` again."
)

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
        .replace(LOAD_ERROR_SLOT, LOAD_ERROR)
        .replace(DRAW_ERROR_SLOT, DRAW_ERROR)
    )


def _escape(value: str) -> str:
    return value.translate(ESCAPES).replace("\n", "<br/>")
