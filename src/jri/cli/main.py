"""CLI entrypoint."""

import os
import sys
from pathlib import Path

from jri.cli.config import load_cli_environment, parse_arguments
from jri.cli.repl import run_repl
from jri.core.agents.factory import (
    create_interviewer,
    validate_interviewer_configuration,
)
from jri.core.config import ConfigError
from jri.core.logging import JsonlLogger
from jri.core.project import find_project_root, initialize_project


def main(argv: list[str] | None = None) -> None:
    """Run the CLI."""
    args = parse_arguments(argv)
    project_root = find_project_root(Path.cwd())
    env = load_cli_environment(cwd=project_root, environ=os.environ)
    os.environ.update(env)
    try:
        validate_interviewer_configuration(env)
    except ConfigError as exc:
        sys.stderr.write(f"{exc}\n")
        raise SystemExit(1) from exc

    try:
        state = initialize_project(project_root, force=args.force)
    except Exception as exc:
        sys.stderr.write(f"{exc}\n")
        raise SystemExit(1) from exc

    logger = JsonlLogger(state.jri_dir / "logs" / "interview.jsonl")
    interviewer = create_interviewer(
        project_root=state.root,
        logger=logger,
        env=env,
    )
    raise SystemExit(run_repl(state=state, interviewer=interviewer))
