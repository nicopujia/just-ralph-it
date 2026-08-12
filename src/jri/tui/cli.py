import argparse
import logging
import os

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError
from pydantic_core import ErrorDetails

from jri import __version__
from jri.core import logs, visualization
from jri.core.conversation import Conversation
from jri.core.exceptions import PersistenceError
from jri.core.generation import Generation
from jri.core.settings import Settings
from jri.core.workspace import Hold, Reset, Workspace
from jri.lib import browser, files, git, terminal
from jri.lib.providers import codex

from . import copy
from .app import App

# This is the shell status for a process ended by hangup. It is also the status after SIGHUP delivery.
# The status does not identify which event ended the window. Only the `terminal_hung_up` record identifies it.
HANGUP_STATUS = 129
# This is the shell status for an interrupt requested by the user. Do not show a traceback for the requested operation.
INTERRUPTED_STATUS = 130

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
    # `jri chat` starts this command in a separate process. It has no `help` text, so users do not see it as a command.
    # It is not a JRI operation. A manual run would report to a conversation that did not request it.
    subparsers.add_parser("generate")

    arguments = parser.parse_args()
    if arguments.command is None:
        parser.print_help()
        return

    handlers = {
        "init": lambda: _initialize(force=arguments.force, yes=arguments.yes),
        "chat": _chat,
        "view": _view,
        "generate": _generate,
    }

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
    # This catches interrupts while a command waits for a lock, browser, or window.
    # Each question handles its own interrupt.
    except KeyboardInterrupt as interrupt:
        raise SystemExit(INTERRUPTED_STATUS) from interrupt


def _initialize(*, force: bool, yes: bool) -> None:
    workspace = Workspace.find()
    settings = Settings.render()
    if force:
        # Ask the question inside the reset. First report an active window or run that prevents deletion.
        # Keep the project held while the user reads. The answer then applies to the project state at deletion.
        with workspace.open_reset() as reset:
            if not (yes or _confirm_reset(reset)):
                print(copy.FORCE_CANCELLED)
                raise SystemExit(1)
            installation = workspace.install(settings, reset=reset)
    else:
        installation = workspace.install(settings)
    # Create a repository only in the command directory. A workspace in a repository already has its root.
    # A relative path here names `.`.
    if installation.repository_created:
        print(copy.INIT_REPOSITORY)
    reset_copy = copy.INIT_RECREATED if force else copy.INIT_EXISTING
    directory = files.shorten_path(workspace.directory)
    print((copy.INIT_CREATED if installation.created else reset_copy).format(directory=directory))
    print(copy.INIT_NEXT_STEPS.format(settings_file=files.shorten_path(workspace.settings_file)))


def _chat() -> None:
    settings = _load_settings()
    logs.configure(settings)
    settings.llm.validate_authentication()
    # Ask for the project hold before the app starts. Then no other output can draw in the terminal.
    hold = Workspace.find().open_hold()
    if not hold.take() and not _take_over(hold):
        raise SystemExit(1)
    conversation = Conversation(settings)
    app = App(conversation)
    logger.info("started")
    terminal.end_on_hangup(_end_hung_up_window)
    try:
        app.run()
    except BaseException:
        logger.exception("failed")
        raise
    finally:
        hold.release()
        logger.info("finished")
        logging.shutdown()


# This is a run without a window. It takes the project generation lock, not the chat lock.
# The starter window still has the chat lock, notes, and session.
def _generate() -> None:
    settings = _load_settings()
    logs.configure(settings)
    settings.llm.validate_authentication()
    Generation.execute(settings)


def _view() -> None:
    settings = _load_settings()
    logs.configure(settings)
    conversation = Conversation(settings)
    graph = conversation.notebook.graph
    visualization_file = conversation.workspace.visualization_file
    visualization_file.write_text(visualization.render(graph), encoding="utf-8", newline="\n")
    # The browser determines whether it opens. It can fail over SSH, without a browser, or when it covers the terminal.
    opened = browser.open_page(visualization_file.resolve().as_uri())
    print((copy.VIEW_OPENED if opened else copy.VIEW_UNOPENED).format(file=files.shorten_path(visualization_file)))
    print(copy.VIEW_NEXT_STEPS if graph.notes else copy.VIEW_NO_NOTES)


def _load_settings() -> Settings:
    workspace = Workspace.find()
    if not workspace.settings_file.exists():
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
        print(copy.SETTINGS_ERROR.format(errors="\n".join(error_lines)))
        raise SystemExit(1) from error


def _describe_issue(issue: ErrorDetails) -> str:
    setting = ".".join(map(str, issue["loc"])) or "settings"
    if issue["type"] != "extra_forbidden":
        return f"- {setting}: {issue['msg']}"
    # An undeclared setting key is probably a typo for its writer, not the Pydantic schema error.
    suggestion = Settings.suggest_setting(issue["loc"])
    return f"- {setting}: " + (
        copy.UNKNOWN_SETTING_SUGGESTION.format(setting=suggestion) if suggestion else copy.UNKNOWN_SETTING
    )


def _confirm_reset(reset: Reset) -> bool:
    if not reset.paths:
        return True
    print(copy.FORCE_WARNING.format(paths="\n".join(f"- {files.shorten_path(target)}" for target in reset.paths)))
    return _confirm(copy.FORCE_PROMPT)


def _take_over(hold: Hold) -> bool:
    print(copy.WORKSPACE_HELD.format(holder=hold.holder))
    if not _confirm(copy.WORKSPACE_HELD_PROMPT):
        print(copy.WORKSPACE_HELD_KEPT)
        return False
    if not hold.evict():
        print(copy.WORKSPACE_HELD_STANDING)
        return False
    return True


# The window terminal is gone. The event loop can block while it writes to a terminal that cannot read.
# A quit request would not return. Exit the process instead.
# Process exit releases the project hold. The separate run continues and records itself.
# A new window reads that record and ends the turn.
# Reaching this point means SIGHUP did not end the window first. This is the less common case in `lib.terminal`.
def _end_hung_up_window() -> None:
    logger.info("terminal_hung_up")
    os._exit(HANGUP_STATUS)


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt)
    # An empty pipe, runner, editor terminal, or line-ending key provides no answer.
    # No standard input also provides no answer. `input` reports that state as an error.
    except (EOFError, RuntimeError):
        return False
    # An interrupt is any answer except `y`. One question leaves another window running. The other removes nothing.
    # The terminal already shows `^C` by the prompt. Print the result on a new line.
    except KeyboardInterrupt:
        print()
        return False
    return answer.strip().casefold() in {"y", "yes"}


if __name__ == "__main__":
    main()
