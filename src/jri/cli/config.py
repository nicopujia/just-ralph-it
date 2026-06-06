"""Command-line configuration loading."""

import argparse
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from dotenv import dotenv_values


@dataclass(frozen=True)
class CliArgs:
    """Parsed command-line arguments."""

    force: bool


def parse_arguments(argv: list[str] | None = None) -> CliArgs:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="jri",
        description="Interview a project idea into rigorous JRI specs.",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="delete and recreate the active .jri directory before starting",
    )
    namespace = parser.parse_args(argv)
    return CliArgs(force=cast("bool", namespace.force))


def load_cli_environment(
    *,
    cwd: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return process environment plus values from the cwd .env file."""
    env = dict(os.environ if environ is None else environ)
    for key, value in dotenv_values(cwd / ".env").items():
        if value is not None:
            env.setdefault(key, value)
    return env
