import argparse
import json
import logging
import sys
import textwrap
import webbrowser

from jri.core.exceptions import AuthError
from jri.core.service import Service
from jri.core.settings import Settings

from .app import App
from .utils import get_settings_or_print_error

logger = logging.getLogger(__name__)


def main() -> None:
    handlers = {"view": _view, None: _run}
    command = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in handlers else None
    if command:
        parser = argparse.ArgumentParser(prog=f"jri {command}")
        parser.parse_args(sys.argv[2:])
        del sys.argv[1:]

    settings = get_settings_or_print_error()
    try:
        handlers[command](settings)
    except AuthError as error:
        print(f"Authentication failed: {error}")
        raise SystemExit(1) from error


def _run(settings: Settings) -> None:
    settings.validate_authentication()
    service = Service(settings)
    app = App(service)
    logger.info("started")
    try:
        app.run()
    except BaseException:
        logger.exception("failed")
        raise
    finally:
        logger.info("finished")
        logging.shutdown()


def _view(settings: Settings) -> None:
    service = Service(settings)
    graph = json.loads(service.graph_file.read_text())
    diagram = ["flowchart TD"]

    for note in graph["notes"]:
        text = note["text"].replace('"', "#quot;").replace("\n", "<br/>")
        diagram.append(f'  {note["id"]}["{text}"]')

    for connection in graph["connections"]:
        label = connection["label"].replace('"', "#quot;").replace("\n", "<br/>")
        diagram.append(f'  {connection["source_id"]} -->|"{label}"| {connection["target_id"]}')

    diagram_content = textwrap.indent("\n".join(diagram), "            ")
    service.graph_visualization_file.write_text(
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
                <pre class="mermaid">{diagram_content}</pre>
            </body>
            </html>
        """)
    )
    print(service.graph_visualization_file)
    webbrowser.open(service.graph_visualization_file.resolve().as_uri())


if __name__ == "__main__":
    main()
