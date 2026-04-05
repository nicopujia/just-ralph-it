import argparse
import subprocess
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
            case "upgrade":
                directory = (working_directory / args.directory).resolve()
                upgrade_service = JriService(directory)
                upgrade_service.upgrade(commit_message=_command_message(argv))
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
                        task_timeout=args.task_timeout,
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
                if not args.force:
                    # Gather information for the confirmation prompt
                    state = service.state_store.load()
                    iteration_number = state.iteration_number
                    if iteration_number < 1:
                        print(
                            "Error: no successful iteration exists yet",
                            file=sys.stderr,
                        )
                        return 1

                    target_tag = f"jri/{iteration_number}"
                    has_uncommitted = bool(service.git.status_short())
                    ralph_branches = [
                        b
                        for b in service.git.run(
                            "branch", "--format=%(refname:short)"
                        ).stdout.splitlines()
                        if b.startswith("ralph/")
                    ]

                    # Build the confirmation message
                    parts = [f"This will reset to {target_tag}."]
                    if has_uncommitted:
                        parts.append("Uncommitted changes will be discarded.")
                    if ralph_branches:
                        parts.append(
                            f"{len(ralph_branches)} ralph/* branch(es) will be deleted."
                        )
                    parts.append("Are you sure? [y/N]")

                    print(" ".join(parts))
                    response = input().strip().lower()
                    if response != "y":
                        print("Reset aborted.", file=sys.stderr)
                        return 1

                service.reset()
                return 0
            case "status":
                if args.json:
                    import json

                    payload = service.structured_status()
                    print(json.dumps(payload, indent=2, sort_keys=True))
                    return 0
                tasks_by_status = service.status()
                total = sum(len(t) for t in tasks_by_status.values())
                print(f"Tasks: {total} total\n")
                max_label = max(len(s) for s in tasks_by_status)
                for status, tasks in tasks_by_status.items():
                    print(f"  {status:<{max_label}}  {len(tasks)}")

                # Collect all tasks assigned to Human across all statuses
                human_tasks = sorted(
                    (
                        (status, t)
                        for status, tasks in tasks_by_status.items()
                        for t in tasks
                        if t.metadata.assignee == "Human"
                    ),
                    key=lambda x: (x[1].metadata.priority, x[0], x[1].slug),
                )
                print("\nTasks assigned to Human:\n")
                if human_tasks:
                    for status, task in human_tasks:
                        p = task.metadata.priority
                        title = task.metadata.title
                        print(f"  [{status:<6}] [P{p}] {task.slug} — {title}")
                else:
                    print("  No tasks assigned to Human.")
                return 0
            case "promote":
                promoted = service.promote_drafts(
                    slugs=args.slugs,
                    user_confirmation=args.confirm or "",
                )
                print(f"Promoted {len(promoted)} draft task(s) to todo.")
                for task in promoted:
                    print(f"  - {task.slug}")
                return 0
            case "timeline":
                from ..core.timeline import TimelineStore

                timeline = TimelineStore(service.paths.timeline_path)
                events = timeline.read()
                if args.iteration is not None:
                    events = [e for e in events if e.iteration == args.iteration]
                if args.task:
                    events = [e for e in events if e.task == args.task]
                if args.json:
                    for event in events:
                        print(event.to_jsonl())
                else:
                    for event in events:
                        parts = [event.ts, event.event]
                        if event.iteration is not None:
                            parts.append(f"iter={event.iteration}")
                        if event.task is not None:
                            parts.append(f"task={event.task}")
                        if event.detail:
                            detail_str = " ".join(
                                f"{k}={v}" for k, v in event.detail.items()
                            )
                            parts.append(detail_str)
                        print(" ".join(parts))
                return 0
            case _:
                parser.print_help()
                return 1
    except JriError as error:
        print(str(error), file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        cmd = " ".join(error.cmd) if isinstance(error.cmd, list) else error.cmd
        detail = (error.stderr or "").strip()
        message = f"git command failed: {cmd}"
        if detail:
            message += f"\n{detail}"
        print(message, file=sys.stderr)
        return 1
    except OSError as error:
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

    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help="Refresh JRI-managed generated files for this project.",
        description=(
            "Update bundled agent prompts and other JRI-managed generated "
            "files without touching project tasks."
        ),
    )
    upgrade_parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Target project directory. Defaults to the current directory.",
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
            "tasks left, the iteration limit is reached, a task timeout "
            "occurs, or a stop is requested."
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
    start_parser.add_argument(
        "--task-timeout",
        type=int,
        metavar="SECONDS",
        help="Maximum seconds per task iteration (0 or unset = no limit).",
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
    reset_parser = subparsers.add_parser(
        "reset",
        help="Reset the default branch to the latest successful iteration.",
        description=(
            "Hard-reset the default branch to the latest successful JRI "
            "iteration tag. Discards all uncommitted changes, commits, "
            "and task state since that iteration. Clears in-progress "
            "runtime state (process tracking, active attempt). "
            "Preserves iteration number, session, and attempt history."
        ),
    )
    reset_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Skip confirmation prompt and proceed with reset.",
    )
    status_parser = subparsers.add_parser(
        "status",
        help="Show task counts by status and list human todo tasks.",
        description=(
            "Display the total number of tasks, broken down by status, "
            "and list all todo tasks assigned to Human."
        ),
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        dest="json",
        help="Output machine-readable structured JSON instead of plain text.",
    )
    promote_parser = subparsers.add_parser(
        "promote",
        help="Promote draft tasks to todo after explicit user confirmation.",
        description=(
            "Move draft tasks into todo only when the user has explicitly "
            "confirmed the promotion."
        ),
    )
    promote_parser.add_argument(
        "slugs",
        nargs="*",
        help="Draft task slugs to promote. Defaults to all draft tasks.",
    )
    promote_parser.add_argument(
        "--confirm",
        help="Explicit user confirmation text recorded for this promotion.",
    )

    timeline_parser = subparsers.add_parser(
        "timeline",
        help="Show the execution timeline for this project.",
        description=(
            "Read the execution timeline and display recorded events. "
            "Each line represents a key event from the Ralph run loop."
        ),
    )
    timeline_parser.add_argument(
        "--iteration",
        type=int,
        help="Filter to events for a specific iteration number.",
    )
    timeline_parser.add_argument(
        "--task",
        help="Filter to events for a specific task slug.",
    )
    timeline_parser.add_argument(
        "--json",
        action="store_true",
        dest="json",
        help="Output raw JSONL instead of formatted text.",
    )

    return parser


def _command_message(argv: list[str]) -> str:
    return " ".join(["jri", *argv])
