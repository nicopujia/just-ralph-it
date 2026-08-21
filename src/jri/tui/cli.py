import argparse
import logging
import os
from datetime import datetime
from typing import NoReturn

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError
from pydantic_core import ErrorDetails

from jri import __version__
from jri.core import logs, paths, visualization
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
# A report shows a moment in the local time of the machine that reads it. `--json` keeps the recorded time.
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=copy.TITLE)
    parser.add_argument("-v", "--version", action="version", version=__version__, help=copy.CLI_VERSION_HELP)
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    init_parser = subparsers.add_parser("init", help=copy.CLI_INIT_HELP, description=copy.CLI_INIT_HELP)
    init_parser.add_argument("--force", action="store_true", help=copy.CLI_FORCE_HELP)
    init_parser.add_argument("--yes", action="store_true", help=copy.CLI_YES_HELP)
    init_parser.add_argument("--no-comments", action="store_true", help=copy.CLI_NO_COMMENTS_HELP)
    for name, description in (
        ("chat", copy.CLI_CHAT_HELP),
        ("view", copy.CLI_VIEW_HELP),
        ("start", copy.CLI_START_HELP),
        ("stop", copy.CLI_STOP_HELP),
        ("halt", copy.CLI_HALT_HELP),
    ):
        subparsers.add_parser(name, help=description, description=description)
    status_parser = subparsers.add_parser("status", help=copy.CLI_STATUS_HELP, description=copy.CLI_STATUS_HELP)
    status_parser.add_argument("--json", action="store_true", help=copy.CLI_JSON_HELP)

    arguments = parser.parse_args()
    if arguments.command is None:
        parser.print_help()
        return

    handlers = {
        "init": lambda: _initialize(force=arguments.force, yes=arguments.yes, comments=not arguments.no_comments),
        "chat": _chat,
        "view": _view,
        "start": _start,
        "stop": _stop,
        "halt": _halt,
        "status": lambda: _status(json=arguments.json),
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


def _initialize(*, force: bool, yes: bool, comments: bool) -> None:
    workspace = Workspace.find()
    # Only an installation that writes a settings file starts the project from the global settings.
    writes_settings = force or not workspace.settings_file.exists()
    settings = Settings.render(_load_global_settings() if writes_settings else None, comments=comments)
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
    if installation.commit is not None:
        print(copy.INIT_COMMITTED)
    # A settings file with no comments has no instructions to read.
    if comments:
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


# This is a run without a window. It takes the project generation lock, not the chat lock.
# The command is the run itself: it holds this terminal until the run ends.
# A supervisor that starts it thus owns the run for all its life.
def _start() -> None:
    settings = _load_settings()
    logs.configure(settings)
    settings.llm.validate_authentication()
    # A run says nothing for as long as it works. Say that it began, so a reader knows the wait is the run.
    # The run says when that is, because a refusal must not follow a line that says it started.
    conclusion = Generation.execute(settings, lambda: print(copy.START_BEGAN, flush=True))
    print(
        copy.START_ENDED_DETAIL.format(ending=conclusion.ending, detail=conclusion.detail)
        if conclusion.detail
        else copy.START_ENDED.format(ending=conclusion.ending)
    )
    # A supervisor reads the process status. An ending the run could do nothing about is a failed process.
    if conclusion.failure:
        raise SystemExit(1)


def _stop() -> None:
    print(copy.STOP_ASKED if Generation(_find_workspace()).stop() else copy.STOP_NO_RUN)


def _halt() -> None:
    print(copy.HALT_KILLED if Generation(_find_workspace()).halt() else copy.HALT_NO_RUN)


# This command only reads. It asks for no settings and for no provider account.
# The settings can be broken, and that is when a reader needs this report most.
def _status(*, json: bool) -> None:
    status = Generation(_find_workspace()).read_status()
    if json:
        print(status.model_dump_json())
        return
    lines = []
    # A runner takes its lock before it writes its first line. Report that run, which has no start time yet.
    if status.pid is not None:
        lines.append(
            copy.STATUS_RUNNING.format(pid=status.pid, started=_describe_time(status.started))
            if status.started is not None
            else copy.STATUS_STARTING.format(pid=status.pid)
        )
    # A run that is gone left the step it was on. Report that step in the past, so it does not read as work in hand.
    if status.step and status.step_started is not None:
        step_copy = copy.STATUS_STEP if status.pid is not None else copy.STATUS_LAST_STEP
        lines.append(step_copy.format(step=status.step, started=_describe_time(status.step_started)))
    if status.stopping:
        lines.append(copy.STATUS_STOPPING)
    if status.ending:
        lines.append(copy.STATUS_ENDED.format(ending=status.ending))
    # A journal with no ending and no process is a run that the machine or a halt ended.
    if status.recorded and not status.ending and status.pid is None:
        lines.append(copy.STATUS_INCOMPLETE)
    if status.draft:
        lines.append(copy.STATUS_DRAFT)
    if status.holder is not None:
        lines.append(copy.STATUS_HELD.format(holder=status.holder))
    print("\n".join(lines) if lines else copy.STATUS_IDLE)


def _find_workspace() -> Workspace:
    workspace = Workspace.find()
    if not workspace.settings_file.exists():
        print(copy.WORKSPACE_MISSING)
        raise SystemExit(1)
    return workspace


def _load_settings() -> Settings:
    workspace = _find_workspace()
    load_dotenv(workspace.root / ".env")
    try:
        return Settings.load()
    except (ValidationError, yaml.YAMLError) as error:
        _report_settings_error(error, files.shorten_path(workspace.settings_file), copy.SETTINGS_ERROR_PROJECT_USE)


# A global settings file that JRI cannot read stops the installation. A default that disappears silently is worse
# than an installation that stops.
def _load_global_settings() -> Settings | None:
    try:
        return Settings.load_global()
    except (ValidationError, yaml.YAMLError) as error:
        _report_settings_error(error, paths.GLOBAL_SETTINGS_FILE, copy.SETTINGS_ERROR_GLOBAL_USE)


# The message names the file that JRI read, and says what that file is for.
def _report_settings_error(error: ValidationError | yaml.YAMLError, settings_file: str, use: str) -> NoReturn:
    error_lines = (
        [_describe_issue(issue) for issue in error.errors()] if isinstance(error, ValidationError) else [f"- {error}"]
    )
    print(copy.SETTINGS_ERROR.format(file=settings_file, errors="\n".join(error_lines), use=use))
    raise SystemExit(1) from error


def _describe_time(moment: datetime) -> str:
    return moment.astimezone().strftime(TIME_FORMAT)


def _describe_issue(issue: ErrorDetails) -> str:
    setting = ".".join(map(str, issue["loc"])) or "settings"
    if issue["type"] != "extra_forbidden":
        return f"- {setting}: {issue['msg']}"
    # An undeclared setting key is probably a typo for its writer, not the Pydantic schema error.
    suggestion = Settings.suggest(issue["loc"])
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
