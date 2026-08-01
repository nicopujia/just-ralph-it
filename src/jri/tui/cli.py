import argparse
import html
import logging
import webbrowser
from contextlib import chdir
from pathlib import Path

import yaml
from dotenv import find_dotenv
from pydantic import ValidationError
from pydantic_settings import CliSettingsSource, SettingsError

from jri.core.exceptions import AuthError, PersistenceError
from jri.core.service import Service
from jri.core.settings import Settings, initialize_workspace

from .app import App
from .constants import CONFIG_ERROR_COPY

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Just Ralph It")
    parser.add_subparsers(dest="command").add_parser(
        "view", help="Visualize the notes graph using the standard JRI configuration."
    )

    settings_source = CliSettingsSource(Settings, root_parser=parser, parse_args_method=parser.parse_args)

    args = parser.parse_args()
    settings_source(parsed_args=args)
    project_dir = Path(getattr(args, "cwd", None) or Path.cwd()).resolve()
    initialize_workspace(project_dir)

    try:
        with chdir(project_dir):
            settings = Settings(
                cwd=project_dir,
                _env_file=find_dotenv(usecwd=True),  # pyright: ignore[reportCallIssue]
                _cli_settings_source=settings_source,  # pyright: ignore[reportCallIssue]
            )
    except (SettingsError, ValidationError, yaml.YAMLError) as error:
        error_lines = (
            [f"- {'.'.join(map(str, issue['loc'])) or 'configuration'}: {issue['msg']}" for issue in error.errors()]
            if isinstance(error, ValidationError)
            else [f"- {error}"]
        )
        print(CONFIG_ERROR_COPY.format(errors="\n".join(error_lines)))
        raise SystemExit(1) from error

    handlers = {"view": _view, None: _run}

    try:
        handlers[args.command](settings)
    except AuthError as error:
        print(f"Authentication failed: {error}")
        raise SystemExit(1) from error
    except PersistenceError as error:
        print(f"Persistence failed: {error}")
        raise SystemExit(1) from error


def _run(settings: Settings) -> None:
    settings.llm.validate_authentication()
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
    graph = service.interviewer.notebook.graph
    diagram = ["flowchart TD", "    classDef topic fill:#fff3cd,stroke:#856404,stroke-width:2px"]
    indentation = "    " * 3

    for topic in graph.topics:
        summary = topic.summary
        label = f"{_escape(topic.name)}<br/>[{topic.status}]"
        if summary:
            label += f"<br/>{_escape(summary)}"
        diagram.append(f'{indentation}{topic.id}(["{label}"]):::topic')

    diagram.extend(f'{indentation}{note.id}["{_escape(note.text)}"]' for note in graph.notes)

    connected_pairs = {(connection.source_id, connection.target_id) for connection in graph.connections}
    diagram.extend(
        f'{indentation}{note.topic_id} -->|"contains"| {note.id}'
        for note in graph.notes
        if (note.topic_id, note.id) not in connected_pairs
    )

    diagram.extend(
        f'{indentation}{connection.source_id} -->|"{_escape(connection.label)}"| {connection.target_id}'
        for connection in graph.connections
    )

    diagram_content = "\n".join(diagram)
    service.visualization_file.write_text(f"""\
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
</html>""")

    print(service.visualization_file)
    webbrowser.open(service.visualization_file.resolve().as_uri())


def _escape(value: str) -> str:
    return html.escape(value, quote=True).replace("\n", "<br/>")


if __name__ == "__main__":
    main()
