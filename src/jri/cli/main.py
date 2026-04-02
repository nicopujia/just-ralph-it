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
    parser = argparse.ArgumentParser(prog="jri")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("directory", nargs="?", default=".")
    init_parser.add_argument("-f", "--force", action="store_true")

    subparsers.add_parser("chat")

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("-n", "--iterations", type=int)
    start_parser.add_argument("-d", "--detached", action="store_true")
    start_parser.add_argument("-m", "--model")

    stop_parser = subparsers.add_parser("stop")
    stop_parser.add_argument("reason", nargs="?")

    subparsers.add_parser("halt")
    subparsers.add_parser("reset")
    return parser


def _command_message(argv: list[str]) -> str:
    return " ".join(["jri", *argv])
