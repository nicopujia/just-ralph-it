#!/usr/bin/env -S uv run --script

"""Convert a JRI graph JSON file into an HTML Mermaid diagram."""

from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph", type=Path, help="Path to the JRI graph JSON file.")
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="Path for the generated HTML file. Defaults to the graph path with an .html extension.",
    )
    arguments = parser.parse_args()

    graph = json.loads(arguments.graph.read_text())
    output = arguments.output or arguments.graph.with_suffix(".html")
    notes = {note["id"]: note["text"] for note in graph["notes"]}
    diagram = ["flowchart TD"]

    for note_id, text in notes.items():
        diagram.append(f'  {note_id}["{escape(text)}"]')

    for connection in graph["connections"]:
        source_id = connection["source_id"]
        target_id = connection["target_id"]
        label = escape(connection["label"])
        diagram.append(f'  {source_id} -->|"{label}"| {target_id}')

    diagram_content = textwrap.indent("\n".join(diagram), "            ")
    output.write_text(
        textwrap.dedent(f"""\
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <script type="module">
                import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
                mermaid.initialize({{ startOnLoad: true }});
              </script>
            </head>
            <body>
              <pre class="mermaid">
                {diagram_content}
              </pre>
            </body>
            </html>
        """)
    )


def escape(text: str) -> str:
    return text.replace('"', "#quot;").replace("\n", "<br/>")


if __name__ == "__main__":
    main()
