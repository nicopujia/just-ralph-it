import argparse
import html
import logging
import webbrowser
from contextlib import chdir
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError
from pydantic_settings import CliSettingsSource, SettingsError

from jri import __version__
from jri.core import paths
from jri.core.exceptions import PersistenceError
from jri.core.service import Service
from jri.core.settings import Settings
from jri.lib import git
from jri.lib.providers import codex

from .app import App
from .constants import (
    CLI_EPILOG_COPY,
    CONFIG_ERROR_COPY,
    FORCE_CANCELLED_COPY,
    FORCE_PROMPT_COPY,
    FORCE_WARNING_COPY,
    INIT_CREATED_COPY,
    INIT_EXISTING_COPY,
    INIT_NEXT_STEPS_COPY,
    INIT_RECREATED_COPY,
    INIT_REPOSITORY_COPY,
    WORKSPACE_MISSING_COPY,
)

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Just Ralph It", epilog=CLI_EPILOG_COPY)
    parser.add_argument("-v", "--version", action="version", version=__version__, help="Show the JRI version and exit.")
    # A positional command, rather than subparsers, lets every setting
    # below be given before or after it.
    parser.add_argument(
        "command",
        nargs="?",
        choices=("init", "chat", "view"),
        help=(
            "init: set the project up with the default JRI configuration. "
            "chat: chat with the interviewer in the terminal UI. "
            "view: visualize the notes graph."
        ),
    )

    settings_source = CliSettingsSource(Settings, root_parser=parser, parse_args_method=parser.parse_args)

    args = parser.parse_args()
    settings_source(parsed_args=args)
    location = Path(getattr(args, "cwd", None) or Path.cwd()).resolve()
    project_dir = git.find_root(location) or location

    if args.command is None:
        parser.print_help()
        return
    if args.command != "init" and not (project_dir / paths.CONFIG_FILE).exists():
        print(WORKSPACE_MISSING_COPY)
        raise SystemExit(1)

    load_dotenv(project_dir / ".env")

    try:
        with chdir(project_dir):
            settings = Settings(
                cwd=project_dir,
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

    if settings.force and not _confirm_reset(args.command, project_dir):
        print(FORCE_CANCELLED_COPY)
        raise SystemExit(1)

    handlers = {"init": _init, "chat": _chat, "view": _view}

    try:
        handlers[args.command](settings)
    except codex.AuthError as error:
        print(f"Authentication failed: {error}")
        raise SystemExit(1) from error
    except git.Error as error:
        print(f"Git failed: {error}")
        raise SystemExit(1) from error
    except PersistenceError as error:
        print(f"Persistence failed: {error}")
        raise SystemExit(1) from error


def _init(settings: Settings) -> None:
    workspace = Service.init(settings.cwd, force=settings.force)
    if workspace.repository_created:
        print(INIT_REPOSITORY_COPY.format(directory=settings.cwd))
    if workspace.created:
        created_copy = INIT_CREATED_COPY
    else:
        created_copy = INIT_RECREATED_COPY if settings.force else INIT_EXISTING_COPY
    print(created_copy.format(directory=workspace.directory))
    print(INIT_NEXT_STEPS_COPY.format(config_file=workspace.config_file))


def _chat(settings: Settings) -> None:
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
    graph = service.notebook.graph
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


def _confirm_reset(command: str, project_dir: Path) -> bool:
    """Ask the user before a forced run throws their files away.

    Returns:
        Whether the command may go ahead.
    """

    targets = (paths.CONFIG_FILE,) if command == "init" else paths.RESET_PATHS
    existing = [target for target in targets if (project_dir / target).exists()]
    if not existing:
        return True
    print(FORCE_WARNING_COPY.format(paths="\n".join(f"- {project_dir / target}" for target in existing)))
    try:
        return input(FORCE_PROMPT_COPY).strip().casefold() in {"y", "yes"}
    except EOFError:
        return False


def _escape(value: str) -> str:
    return html.escape(value, quote=True).replace("\n", "<br/>")


if __name__ == "__main__":
    main()
