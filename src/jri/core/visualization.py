from .notes import Graph

DRAW_ERROR = (
    "The graph viewer loaded, but it could not draw the graph. Report it at "
    "https://github.com/nicopujia/just-ralph-it/issues."
)
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
                    mermaid.initialize({ startOnLoad: false });
                    await mermaid.run();
                    // The freshly inserted SVG has no layout yet. Sizing the
                    // pan and zoom now would measure it as 0x0 and scale the
                    // graph away to nothing.
                    await new Promise((paint) => requestAnimationFrame(paint));
                    window.svgPanZoom(document.querySelector(".mermaid svg"), { controlIconsEnabled: true });
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
