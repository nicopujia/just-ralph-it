import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

from ..core.errors import JriError, RestartRequested
from ..core.git import MSG_INIT
from ..core.providers import provider_choices, resolve_provider_models
from ..core.service import JriService

_ALLOW_SELF_RESTART_ENV = "JRI_ALLOW_SELF_RESTART"
_INTERNAL_RUN_LOOP_ENV = "JRI_INTERNAL_RUN_LOOP"
_REMAINING_TASKS_ENV = "JRI_REMAINING_TASKS"

_COMMAND_MESSAGES = {
    "halt": "tracked Ralph process stopped.",
    "init": "initialization complete.",
    "reset": "reset complete.",
    "start": "Ralph is running in the background. Use `jri attach` to follow progress.",
    "stop": "stop requested; Ralph will stop after the current task.",
}


def main(argv: list[str] | None = None, *, cwd: Path | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    working_directory = cwd or Path.cwd()
    service = JriService(working_directory)

    if os.environ.get(_INTERNAL_RUN_LOOP_ENV) == "1":
        args = _build_internal_run_loop_parser().parse_args(argv)
        args.max_tasks = _override_remaining_tasks(args.max_tasks)
        if args.dogfood:
            os.environ[_ALLOW_SELF_RESTART_ENV] = "1"
        try:
            return (
                0
                if service.run_loop_process(
                    max_tasks=args.max_tasks,
                    model=args.model,
                    validator_model=args.validator_model,
                    general_model=args.general_model,
                    explore_model=args.explore_model,
                    task_timeout=args.task_timeout,
                    force=args.force,
                    dogfood=args.dogfood,
                )
                >= 0
                else 1
            )
        except RestartRequested as restart:
            _restart_internal_run_loop(argv, remaining_tasks=restart.remaining_tasks)

    parser = _build_parser()
    args, unknown = parser.parse_known_args(argv)

    if args.command != "chat" and unknown:
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")

    try:
        match args.command:
            case "chat":
                chat_models = resolve_provider_models(
                    args.provider,
                    mode="chat",
                    overrides={
                        "model": args.model,
                        "validator_model": args.validator_model,
                        "explore_model": args.explore_model,
                    },
                )
                return _finalize_command_return(
                    "chat",
                    service.chat(
                        unknown,
                        fresh=args.fresh,
                        model=chat_models["model"],
                        validator_model=chat_models["validator_model"],
                        explore_model=chat_models["explore_model"],
                    ),
                )
            case "status":
                tasks_by_status = service.status()
                total = sum(len(t) for t in tasks_by_status.values())
                print(f"Tasks: {total} total")
                print(service.ralph_status_summary())
                print()
                max_label = max(len(s) for s in tasks_by_status)
                for status, tasks in tasks_by_status.items():
                    print(f"  {status:<{max_label}}  {len(tasks)}")

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
                metrics = service.metrics_summary()
                if metrics:
                    print(f"\n{metrics}")
                return 0
            case "timeline":
                from ..core.timeline import TimelineStore

                timeline = TimelineStore(service.paths.timeline_path)
                events = timeline.read()
                if args.task:
                    events = [e for e in events if e.task == args.task]
                if args.json:
                    if not events:
                        print("[]")
                    else:
                        for event in events:
                            print(event.to_jsonl())
                elif not events:
                    print("No timeline events recorded.")
                else:
                    for event in events:
                        parts = [event.ts, event.event]
                        if event.task is not None:
                            parts.append(f"task={event.task}")
                        if event.detail:
                            detail_str = " ".join(
                                f"{k}={v}" for k, v in event.detail.items()
                            )
                            parts.append(detail_str)
                        print(" ".join(parts))
                return 0
            case "inspect":
                service.inspect(args.slug)
                return 0
            case "init":
                directory = (working_directory / args.directory).resolve()
                init_service = JriService(directory)
                init_service.init(
                    delete=args.delete,
                    commit_message=MSG_INIT,
                )
                _print_command_message("init")
                return 0
            case "start":
                start_models = resolve_provider_models(
                    args.provider,
                    mode="start",
                    overrides={
                        "model": args.model,
                        "validator_model": args.validator_model,
                        "general_model": args.general_model,
                        "explore_model": args.explore_model,
                    },
                )
                if args.detached:
                    result = service.start(
                        max_tasks=args.max_tasks,
                        detached=True,
                        model=start_models["model"],
                        validator_model=start_models["validator_model"],
                        general_model=start_models["general_model"],
                        explore_model=start_models["explore_model"],
                        task_timeout=args.task_timeout,
                        force=args.force,
                        dogfood=args.dogfood,
                    )
                    if result >= 0:
                        _print_command_message("start")
                        return 0
                    return _finalize_command_return("start", -result)
                return _finalize_command_return(
                    "start",
                    service.start_attached(
                        max_tasks=args.max_tasks,
                        model=start_models["model"],
                        validator_model=start_models["validator_model"],
                        general_model=start_models["general_model"],
                        explore_model=start_models["explore_model"],
                        task_timeout=args.task_timeout,
                        force=args.force,
                        dogfood=args.dogfood,
                    ),
                )
            case "stop":
                service.stop(args.reason)
                _print_command_message("stop")
                return 0
            case "halt":
                service.halt()
                _print_command_message("halt")
                return 0
            case "reset":
                if not args.force:
                    target_tag = service._resolve_reset_target_tag(args.task)

                    has_uncommitted = bool(service.git.status_short())
                    has_ralph = service.git.has_local_branch("ralph")

                    parts = [f"This will reset to {target_tag}."]
                    if has_uncommitted:
                        parts.append("Uncommitted changes will be discarded.")
                    if has_ralph:
                        parts.append("The ralph branch and worktree will be deleted.")
                    parts.append("Are you sure? [y/N]")

                    print(" ".join(parts))
                    response = input().strip().lower()
                    if response != "y":
                        print("Reset aborted.", file=sys.stderr)
                        return 1

                service.reset(target_task=args.task)
                _print_command_message("reset")
                return 0
            case "attach":
                service.attach()
                return 0
            case _:
                parser.print_help()
                return 1
    except JriError as error:
        _print_command_error(args.command, str(error))
        return 1
    except subprocess.CalledProcessError as error:
        cmd = " ".join(error.cmd) if isinstance(error.cmd, list) else error.cmd
        detail = (error.stderr or "").strip()
        message = f"git command failed: {cmd}"
        if detail:
            message += f"\n{detail}"
        _print_command_error(args.command, message)
        return 1
    except OSError as error:
        _print_command_error(args.command, str(error))
        return 1


def _print_command_message(command: str) -> None:
    print(f"{command}: {_COMMAND_MESSAGES[command]}")


def _finalize_command_return(command: str, returncode: int) -> int:
    if returncode != 0:
        _print_command_error(command, f"command exited with status {returncode}")
    return returncode


def _print_command_error(command: str | None, message: str) -> None:
    prefix = f"{command}: " if command else ""
    print(f"{prefix}{message}", file=sys.stderr)


def _override_remaining_tasks(max_tasks: int | None) -> int | None:
    remaining = os.environ.get(_REMAINING_TASKS_ENV)
    if remaining is None:
        return max_tasks
    try:
        return int(remaining)
    except ValueError:
        return max_tasks


def _restart_internal_run_loop(
    argv: list[str], *, remaining_tasks: int | None
) -> NoReturn:
    env = os.environ.copy()
    env[_ALLOW_SELF_RESTART_ENV] = "1"
    if remaining_tasks is None:
        env.pop(_REMAINING_TASKS_ENV, None)
    else:
        env[_REMAINING_TASKS_ENV] = str(remaining_tasks)
    os.execve(sys.executable, [sys.executable, "-m", "jri", *argv], env)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jri",
        description=(
            "Manage a project and run the Ralph task loop. "
            "Use 'jri <command> --help' for command-specific details."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="command")
    # Keep public command registrations alphabetized so top-level help stays sorted.

    subparsers.add_parser(
        "attach",
        help="Attach to the tracked Ralph runtime.",
        description="Attach to the tracked Ralph runtime.",
    )

    chat_parser = subparsers.add_parser(
        "chat",
        help="Open an interactive chat session for this project.",
        description=(
            "Launch chat in the current project, reusing the saved session "
            "when one is available."
        ),
    )
    chat_parser.add_argument(
        "--fresh",
        "--new",
        action="store_true",
        help="Clear the existing interrogator session and start fresh.",
    )
    chat_parser.add_argument(
        "--provider",
        choices=provider_choices(),
        help=(
            "Apply the named model provider bundle for chat. "
            "Use 'default' to match the checked-in config."
        ),
    )
    chat_parser.add_argument(
        "-m",
        "--model",
        help="Override the interrogator model for this chat run only.",
    )
    chat_parser.add_argument(
        "--validator-model",
        help="Override the interrogator-validator model for this chat run only.",
    )
    chat_parser.add_argument(
        "--explore-model",
        help="Override the explore subagent model for this chat run only.",
    )

    subparsers.add_parser(
        "halt",
        help="Terminate the currently tracked Ralph process immediately.",
        description=(
            "Send SIGTERM to the tracked Ralph loop and clear its tracked "
            "process state."
        ),
    )

    init_parser = subparsers.add_parser(
        "init",
        help="Initialize JRI in the current git repo.",
        description=("Create the .jri scaffold and initial state for this project."),
    )
    init_parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Target project directory. Defaults to the current directory.",
    )
    init_modes = init_parser.add_mutually_exclusive_group()
    init_modes.add_argument(
        "-f",
        "--force",
        "--delete",
        action="store_true",
        dest="delete",
        help=("Skip prompts and overwrite existing .jri/ contents."),
    )

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="Inspect a task slug.",
        description="Inspect a task by slug.",
    )
    inspect_parser.add_argument(
        "slug",
        nargs="?",
        help="Task slug to inspect. Defaults to the active or latest attempt.",
    )

    reset_parser = subparsers.add_parser(
        "reset",
        help="Reset the default branch to the latest task tag.",
        description=(
            "Hard-reset the default branch to the latest task tag. "
            "By default, resets to the most recent jri/end/{task} tag. "
            "Optionally specify a task slug to reset to a specific task's end tag. "
            "Discards all uncommitted changes, commits, "
            "and task state since that task. Clears in-progress "
            "runtime state (process tracking, active attempt). "
            "Preserves session and attempt history."
        ),
    )
    reset_parser.add_argument(
        "task",
        nargs="?",
        help="Optional task slug to reset to a specific task's end tag.",
    )
    reset_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Skip confirmation prompt and proceed with reset.",
    )

    start_parser = subparsers.add_parser(
        "start",
        help="Run Ralph on queued todo tasks.",
        description=(
            "Run the Ralph loop on eligible todo tasks until there are no "
            "tasks left, the task limit is reached, a task timeout "
            "occurs, or a stop is requested."
        ),
    )
    start_parser.add_argument(
        "-n",
        "--tasks",
        type=int,
        dest="max_tasks",
        help="Maximum number of tasks to run in this invocation.",
    )
    start_parser.add_argument(
        "-d",
        "--detached",
        action="store_true",
        help="Run the loop in the background and track it in .jri/state.json.",
    )
    start_parser.add_argument(
        "--provider",
        choices=provider_choices(),
        help=(
            "Apply the named model provider bundle for start. "
            "Use 'default' to match the checked-in config."
        ),
    )
    start_parser.add_argument(
        "-m",
        "--model",
        help="Override the Ralph model for this start run only.",
    )
    start_parser.add_argument(
        "--validator-model",
        help="Override the Ralph validator model for this start run only.",
    )
    start_parser.add_argument(
        "--general-model",
        help="Override the general subagent model for this start run only.",
    )
    start_parser.add_argument(
        "--explore-model",
        help="Override the explore subagent model for this start run only.",
    )
    start_parser.add_argument(
        "--task-timeout",
        type=int,
        metavar="SECONDS",
        help="Maximum seconds per task (0 or unset = no limit).",
    )
    start_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Auto-resolve pre-flight checks without interactive prompts.",
    )
    start_parser.add_argument(
        "--dogfood",
        action="store_true",
        help=(
            "Restart JRI between completed iterations so the next task uses "
            "updated JRI code."
        ),
    )

    subparsers.add_parser(
        "status",
        help="Show task counts by status and list human todo tasks.",
        description=(
            "Display the total number of tasks, broken down by status, "
            "and list all todo tasks assigned to Human."
        ),
    )
    stop_parser = subparsers.add_parser(
        "stop",
        help="Ask Ralph to stop after the current task.",
        description=(
            "Write a stop signal that prevents the next Ralph task from starting."
        ),
    )
    stop_parser.add_argument(
        "reason",
        nargs="?",
        help="Optional text written into the stop signal file.",
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


def _build_internal_run_loop_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jri", add_help=False)
    parser.add_argument("-n", "--tasks", type=int, dest="max_tasks")
    parser.add_argument("-m", "--model")
    parser.add_argument("--validator-model")
    parser.add_argument("--general-model")
    parser.add_argument("--explore-model")
    parser.add_argument("--task-timeout", type=int, metavar="SECONDS")
    parser.add_argument("-f", "--force", action="store_true")
    parser.add_argument("--dogfood", action="store_true")
    return parser
