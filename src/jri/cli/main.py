import argparse
import sys
from pathlib import Path

from ..core.errors import JriError
from ..core.service import JriService


def main(argv: list[str] | None = None, *, cwd: Path | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args, unknown = parser.parse_known_args(argv)
    working_directory = cwd or Path.cwd()
    service = JriService(working_directory)

    try:
        match args.command:
            case "init":
                directory = (working_directory / args.directory).resolve()
                init_service = JriService(directory)
                init_service.init(
                    force=args.force, commit_message=_command_message(argv)
                )
                return 0
            case "chat":
                return service.chat(unknown)
            case "start":
                return (
                    0
                    if service.start(
                        iterations=args.iterations,
                        detached=args.detached,
                        model=args.model,
                    )
                    >= 0
                    else 1
                )
            case "stop":
                service.stop(args.reason)
                return 0
            case "halt":
                service.halt()
                return 0
            case "reset":
                service.reset()
                return 0
            case _:
                parser.print_help()
                return 1
    except JriError as error:
        print(str(error), file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jri",
        description=(
            "Manage a Just Ralph It project and run the Ralph task loop. "
            "Use 'jri <command> --help' for command-specific details."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    init_parser = subparsers.add_parser(
        "init",
        help="Initialize JRI in the current git repo.",
        description=(
            "Create the .jri scaffold, bundled agent prompts, and initial "
            "state for this project."
        ),
    )
    init_parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Target project directory. Defaults to the current directory.",
    )
    init_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Reinitialize the project even if .jri already exists.",
    )

    subparsers.add_parser(
        "chat",
        help="Open an interactive chat session for this project.",
        description=(
            "Launch chat in the current project, reusing the saved session "
            "when one is available."
        ),
    )

    start_parser = subparsers.add_parser(
        "start",
        help="Run Ralph on queued todo tasks.",
        description=(
            "Run the Ralph loop on eligible todo tasks until there are no "
            "tasks left, the iteration limit is reached, or a stop is "
            "requested."
        ),
    )
    start_parser.add_argument(
        "-n",
        "--iterations",
        type=int,
        help="Maximum number of task iterations to run in this invocation.",
    )
    start_parser.add_argument(
        "-d",
        "--detached",
        action="store_true",
        help="Run the loop in the background and track it in .jri/state.json.",
    )
    start_parser.add_argument(
        "-m",
        "--model",
        help="Override the OpenCode model for this start run only.",
    )

    stop_parser = subparsers.add_parser(
        "stop",
        help="Ask Ralph to stop after the current iteration.",
        description=(
            "Write a stop signal that prevents the next Ralph iteration from starting."
        ),
    )
    stop_parser.add_argument(
        "reason",
        nargs="?",
        help="Optional text written into the stop signal file.",
    )

    subparsers.add_parser(
        "halt",
        help="Terminate the currently tracked Ralph process immediately.",
        description=(
            "Send SIGTERM to the tracked Ralph loop and clear its tracked "
            "process state."
        ),
    )
    subparsers.add_parser(
        "reset",
        help="Reset main back to the latest successful JRI iteration.",
        description=(
            "Check out main and hard-reset it to the latest successful JRI "
            "iteration tag."
        ),
    )
    return parser


def _command_message(argv: list[str]) -> str:
    return " ".join(["jri", *argv])
