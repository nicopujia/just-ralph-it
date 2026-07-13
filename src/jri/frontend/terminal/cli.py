import argparse
import html
import json
import logging
import webbrowser

from pydantic import ValidationError
from pydantic_settings import CliSettingsSource

from jri.core.exceptions import AuthError
from jri.core.service import Service
from jri.core.settings import Settings

from .app import App
from .constants import CONFIG_ERROR_COPY

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Just Ralph It")
    parser.add_subparsers(dest="command").add_parser("view", help="Visualize the notes graph.")

    settings_source = CliSettingsSource(Settings, root_parser=parser, parse_args_method=parser.parse_args)

    args = parser.parse_args()

    try:
        settings = Settings(_cli_settings_source=settings_source)  # pyright: ignore[reportCallIssue]
    except ValidationError as error:
        use_cli_kebab_case = Settings.model_config.get("cli_kebab_case")
        env_prefix = Settings.model_config.get("env_prefix", "")
        error_lines: list[str] = []
        for issue in error.errors():
            field_name = str(issue["loc"][0])
            field = Settings.model_fields[field_name]
            cli_name = field_name.replace("_", "-") if use_cli_kebab_case else field_name
            error_lines.append(
                f"- {env_prefix}{field_name.upper()} or --{cli_name}: "
                f"{field.description or '<no description available>'}"
            )
        print(CONFIG_ERROR_COPY.format(errors="\n".join(error_lines)))
        raise SystemExit(1) from error

    handlers = {"view": _view, None: _run}

    try:
        handlers[args.command](settings)
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
    indentation = "    " * 3

    for note in graph["notes"]:
        text = html.escape(note["text"], quote=False).replace('"', "&quot;").replace("\n", "<br/>")
        diagram.append(f'{indentation}{note["id"]}["{text}"]')

    for connection in graph["connections"]:
        label = html.escape(connection["label"], quote=False).replace('"', "&quot;").replace("\n", "<br/>")
        diagram.append(f'{indentation}{connection["source_id"]} -->|"{label}"| {connection["target_id"]}')

    diagram_content = "\n".join(diagram)
    service.graph_visualization_file.write_text(f"""\
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <style>
        html, body, .mermaid, .mermaid svg {{
            width: 100%;
            height: 100%;
            margin: 0;
        }}

        body {{
            overflow: hidden;
        }}
    </style>
    <script src="https://cdn.jsdelivr.net/npm/svg-pan-zoom@3.6.2/dist/svg-pan-zoom.min.js"></script>
    <script type="module">
        import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
        mermaid.initialize({{ startOnLoad: false }});
        await mermaid.run();
        svgPanZoom(document.querySelector(".mermaid svg"), {{ controlIconsEnabled: true }});
    </script>
</head>
<body>
    <pre class="mermaid">
        {diagram_content}
    </pre>
</body>
</html>""")

    print(service.graph_visualization_file)
    webbrowser.open(service.graph_visualization_file.resolve().as_uri())


if __name__ == "__main__":
    main()
