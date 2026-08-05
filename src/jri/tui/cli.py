import argparse
import logging
import webbrowser

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError
from pydantic_core import ErrorDetails

from jri import __version__
from jri.core import logs, visualization
from jri.core.conversation import Conversation
from jri.core.exceptions import PersistenceError
from jri.core.settings import Settings
from jri.core.workspace import Workspace
from jri.lib import git
from jri.lib.providers import codex

from . import copy
from .app import App

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=copy.TITLE)
    parser.add_argument("-v", "--version", action="version", version=__version__, help=copy.CLI_VERSION_HELP)
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    init_parser = subparsers.add_parser("init", help=copy.CLI_INIT_HELP, description=copy.CLI_INIT_HELP)
    init_parser.add_argument("--force", action="store_true", help=copy.CLI_FORCE_HELP)
    init_parser.add_argument("--yes", action="store_true", help=copy.CLI_YES_HELP)
    for name, description in (("chat", copy.CLI_CHAT_HELP), ("view", copy.CLI_VIEW_HELP)):
        subparsers.add_parser(name, help=description, description=description)

    arguments = parser.parse_args()
    if arguments.command is None:
        parser.print_help()
        return

    handlers = {"init": lambda: _initialize(force=arguments.force, yes=arguments.yes), "chat": _chat, "view": _view}

    try:
        handlers[arguments.command]()
    except codex.AuthError as error:
        print(copy.AUTH_ERROR.format(error=error))
        raise SystemExit(1) from error
    except git.Error as error:
        print(copy.GIT_ERROR.format(error=error))
        raise SystemExit(1) from error
    except PersistenceError as error:
        print(copy.PERSISTENCE_ERROR.format(error=error))
        raise SystemExit(1) from error


def _initialize(*, force: bool, yes: bool) -> None:
    workspace = Workspace.find()
    if force and not (yes or _confirm_reset(workspace)):
        print(copy.FORCE_CANCELLED)
        raise SystemExit(1)
    installation = workspace.install(Settings.render_config(), force=force)
    if installation.repository_created:
        print(copy.INIT_REPOSITORY.format(directory=workspace.root))
    reset_copy = copy.INIT_RECREATED if force else copy.INIT_EXISTING
    print((copy.INIT_CREATED if installation.created else reset_copy).format(directory=workspace.directory))
    print(copy.INIT_NEXT_STEPS.format(config_file=workspace.config_file))


def _chat() -> None:
    settings = _load_settings()
    logs.configure(settings)
    settings.llm.validate_authentication()
    conversation = Conversation(settings)
    app = App(conversation)
    logger.info("started")
    try:
        app.run()
    except BaseException:
        logger.exception("failed")
        raise
    finally:
        logger.info("finished")
        logging.shutdown()


def _view() -> None:
    settings = _load_settings()
    logs.configure(settings)
    conversation = Conversation(settings)
    visualization_file = conversation.workspace.visualization_file
    visualization_file.write_text(visualization.render(conversation.notebook.graph), encoding="utf-8")
    print(visualization_file)
    webbrowser.open(visualization_file.resolve().as_uri())


def _load_settings() -> Settings:
    workspace = Workspace.find()
    if not workspace.config_file.exists():
        print(copy.WORKSPACE_MISSING)
        raise SystemExit(1)
    load_dotenv(workspace.root / ".env")
    try:
        return Settings.load()
    except (ValidationError, yaml.YAMLError) as error:
        error_lines = (
            [_describe_issue(issue) for issue in error.errors()]
            if isinstance(error, ValidationError)
            else [f"- {error}"]
        )
        print(copy.CONFIG_ERROR.format(errors="\n".join(error_lines)))
        raise SystemExit(1) from error


def _describe_issue(issue: ErrorDetails) -> str:
    setting = ".".join(map(str, issue["loc"])) or "configuration"
    if issue["type"] != "extra_forbidden":
        return f"- {setting}: {issue['msg']}"
    # A key the settings do not declare reads as a typo to the person
    # who wrote it, not as the schema violation Pydantic reports.
    suggestion = Settings.suggest_setting(issue["loc"])
    return f"- {setting}: " + (
        copy.UNKNOWN_SETTING_SUGGESTION.format(setting=suggestion) if suggestion else copy.UNKNOWN_SETTING
    )


def _confirm_reset(workspace: Workspace) -> bool:
    existing = [target for target in (workspace.config_file, *workspace.reset_paths) if target.exists()]
    if not existing:
        return True
    print(copy.FORCE_WARNING.format(paths="\n".join(f"- {target}" for target in existing)))
    try:
        return input(copy.FORCE_PROMPT).strip().casefold() in {"y", "yes"}
    except EOFError:
        return False


if __name__ == "__main__":
    main()
