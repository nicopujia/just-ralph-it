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

from . import copy
from .app import App

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=copy.TITLE, epilog=copy.CLI_EPILOG)
    parser.add_argument("-v", "--version", action="version", version=__version__, help=copy.CLI_VERSION_HELP)
    # A positional command, rather than subparsers, lets every setting
    # below be given before or after it.
    parser.add_argument("command", nargs="?", choices=("init", "chat", "view"), help=copy.CLI_COMMAND_HELP)

    settings_source = CliSettingsSource(Settings, root_parser=parser, parse_args_method=parser.parse_args)

    args = parser.parse_args()
    settings_source(parsed_args=args)
    location = Path(args.cwd or Path.cwd()).resolve()
    project_dir = git.find_root(location) or location

    if args.command is None:
        parser.print_help()
        return
    if args.command != "init" and not (project_dir / paths.CONFIG_FILE).exists():
        print(copy.WORKSPACE_MISSING)
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
        print(copy.CONFIG_ERROR.format(errors="\n".join(error_lines)))
        raise SystemExit(1) from error

    if settings.force:
        if args.command != "init":
            print(copy.FORCE_COMMAND)
            raise SystemExit(1)
        if not _confirm_reset(project_dir):
            print(copy.FORCE_CANCELLED)
            raise SystemExit(1)

    handlers = {"init": _init, "chat": _chat, "view": _view}

    try:
        handlers[args.command](settings)
    except codex.AuthError as error:
        print(copy.AUTH_ERROR.format(error=error))
        raise SystemExit(1) from error
    except git.Error as error:
        print(copy.GIT_ERROR.format(error=error))
        raise SystemExit(1) from error
    except PersistenceError as error:
        print(copy.PERSISTENCE_ERROR.format(error=error))
        raise SystemExit(1) from error


def _init(settings: Settings) -> None:
    workspace = Service.init(settings.cwd, force=settings.force)
    if workspace.repository_created:
        print(copy.INIT_REPOSITORY.format(directory=settings.cwd))
    if workspace.created:
        created_copy = copy.INIT_CREATED
    else:
        created_copy = copy.INIT_RECREATED if settings.force else copy.INIT_EXISTING
    print(created_copy.format(directory=workspace.directory))
    print(copy.INIT_NEXT_STEPS.format(config_file=workspace.config_file))


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
    path = Service(settings).visualize()
    print(path)
    webbrowser.open(path.resolve().as_uri())


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
