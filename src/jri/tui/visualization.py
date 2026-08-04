"""Render the note graph as a self-contained diagram document."""

import html

from jri.core.notes import Graph

__all__ = ["render"]

INDENTATION = "    " * 3


def render(graph: Graph) -> str:
    """Render the note graph as a pannable Mermaid diagram page.

    Returns:
        An HTML document holding the whole graph.
    """

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

    diagram_content = "\n".join(diagram)
    return f"""\
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <style>
        html, body, .mermaid, .mermaid svg {{
            width: 100%;
            height: 100%;
            max-width: none !important;
            margin: 0;
        }}

        body {{
            overflow: hidden;
        }}
    </style>
    <script src="https://cdn.jsdelivr.net/npm/svg-pan-zoom@3.6.2/dist/svg-pan-zoom.min.js"></script>
    <script type="module">
        try {{
            const {{ default: mermaid }} = await import(
                "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"
            );
            mermaid.initialize({{ startOnLoad: false }});
            await mermaid.run();
            window.svgPanZoom(document.querySelector(".mermaid svg"), {{ controlIconsEnabled: true }});
        }} catch {{
            document.body.textContent = "The graph viewer could not load its CDN resources. "
                + "Check your internet connection.";
        }}
    </script>
</head>
<body>
    <pre class="mermaid">
        {diagram_content}
    </pre>
</body>
</html>"""


def _escape(value: str) -> str:
    return html.escape(value, quote=True).replace("\n", "<br/>")
