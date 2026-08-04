import argparse
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

from . import copy, visualization
from .app import App

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=copy.TITLE, epilog=copy.CLI_EPILOG)
    parser.add_argument("-v", "--version", action="version", version=__version__, help=copy.CLI_VERSION_HELP)
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    init_parser = subparsers.add_parser("init", help=copy.CLI_INIT_HELP, description=copy.CLI_INIT_HELP)
    init_parser.add_argument("--cwd", help=copy.CLI_CWD_HELP)
    init_parser.add_argument("--force", action="store_true", help=copy.CLI_FORCE_HELP)
    init_parser.add_argument("--yes", action="store_true", help=copy.CLI_YES_HELP)
    # A settings source bound to a subparser puts its flags on that
    # command alone, so `init` takes none and every other command
    # rejects the flags it has no use for.
    for name, description in (("chat", copy.CLI_CHAT_HELP), ("view", copy.CLI_VIEW_HELP)):
        subparser = subparsers.add_parser(name, help=description, description=description, epilog=copy.CLI_EPILOG)
        subparser.set_defaults(
            settings_source=CliSettingsSource(Settings, root_parser=subparser, parse_args_method=subparser.parse_args)
        )

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return

    handlers = {"init": _init, "chat": _chat, "view": _view}

    try:
        handlers[args.command](args)
    except codex.AuthError as error:
        print(copy.AUTH_ERROR.format(error=error))
        raise SystemExit(1) from error
    except git.Error as error:
        print(copy.GIT_ERROR.format(error=error))
        raise SystemExit(1) from error
    except PersistenceError as error:
        print(copy.PERSISTENCE_ERROR.format(error=error))
        raise SystemExit(1) from error


def _init(args: argparse.Namespace) -> None:
    location = Path(args.cwd or Path.cwd()).resolve()
    # Creating things is what `init` is for, and Git can only report the
    # repository a directory belongs to once that directory exists.
    location.mkdir(parents=True, exist_ok=True)
    project_dir = git.find_root(location) or location
    if args.force and not (args.yes or _confirm_reset(project_dir)):
        print(copy.FORCE_CANCELLED)
        raise SystemExit(1)
    workspace = Service.init(project_dir, force=args.force)
    if workspace.repository_created:
        print(copy.INIT_REPOSITORY.format(directory=project_dir))
    reset_copy = copy.INIT_RECREATED if args.force else copy.INIT_EXISTING
    print((copy.INIT_CREATED if workspace.created else reset_copy).format(directory=workspace.directory))
    print(copy.INIT_NEXT_STEPS.format(config_file=workspace.config_file))


def _chat(args: argparse.Namespace) -> None:
    settings = _load_settings(args)
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


def _view(args: argparse.Namespace) -> None:
    service = Service(_load_settings(args))
    service.visualization_file.write_text(visualization.render(service.notebook.graph), encoding="utf-8")
    print(service.visualization_file)
    webbrowser.open(service.visualization_file.resolve().as_uri())


def _load_settings(args: argparse.Namespace) -> Settings:
    """Resolve the settings a command runs with.

    Returns:
        The settings of the project the command was pointed at.

    Raises:
        SystemExit: If the directory is missing, the project has no
            workspace, or its configuration cannot be resolved.
    """

    location = Path(getattr(args, "cwd", None) or Path.cwd()).resolve()
    if not location.is_dir():
        print(copy.DIRECTORY_MISSING.format(directory=location))
        raise SystemExit(1)
    project_dir = git.find_root(location) or location
    if not (project_dir / paths.CONFIG_FILE).exists():
        print(copy.WORKSPACE_MISSING)
        raise SystemExit(1)
    load_dotenv(project_dir / ".env")
    args.settings_source(parsed_args=args)
    try:
        with chdir(project_dir):
            return Settings(
                cwd=project_dir,
                _cli_settings_source=args.settings_source,  # pyright: ignore[reportCallIssue]
            )
    except (SettingsError, ValidationError, yaml.YAMLError) as error:
        error_lines = (
            [f"- {'.'.join(map(str, issue['loc'])) or 'configuration'}: {issue['msg']}" for issue in error.errors()]
            if isinstance(error, ValidationError)
            else [f"- {error}"]
        )
        print(copy.CONFIG_ERROR.format(errors="\n".join(error_lines)))
        raise SystemExit(1) from error


def _confirm_reset(project_dir: Path) -> bool:
    """Ask the user before a forced run throws their files away.

    Returns:
        Whether the command may go ahead.
    """

    targets = (paths.CONFIG_FILE, *paths.RESET_PATHS)
    existing = [target for target in targets if (project_dir / target).exists()]
    if not existing:
        return True
    print(copy.FORCE_WARNING.format(paths="\n".join(f"- {project_dir / target}" for target in existing)))
    try:
        return input(copy.FORCE_PROMPT).strip().casefold() in {"y", "yes"}
    except EOFError:
        return False


if __name__ == "__main__":
    main()
