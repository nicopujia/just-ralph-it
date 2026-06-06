#!/usr/bin/env -S uv run --script
"""Run the project validation workflow."""

import argparse
import shutil
import subprocess
import sys
from typing import cast

FULL_MODE = "full"
SMOKE_MODE = "smoke"
FAST_MODE = "fast"

uv = shutil.which("uv")
if uv is None:
    msg = "uv is not available"
    raise RuntimeError(msg)

parser = argparse.ArgumentParser(
    description="Run the project validation workflow."
)
_action = parser.add_argument(
    "--mode",
    choices=(FULL_MODE, SMOKE_MODE, FAST_MODE),
    default=SMOKE_MODE,
    help=(
        "Fast: formatter + linter + typechecker."
        + " Smoke: fast + non-live tests."
        + " Full: fast + live tests."
    ),
)
mode = cast("str", parser.parse_args().mode)

commands: list[tuple[str, ...]] = [
    (uv, "run", "--locked", "ruff", "format", "-q"),
    (uv, "run", "--locked", "ruff", "check", "--fix", "-q"),
    (uv, "run", "--locked", "basedpyright"),
]

if mode in {SMOKE_MODE, FULL_MODE}:
    test_cmd = (
        uv,
        "run",
        "--locked",
        "coverage",
        "run",
        "-m",
        "pytest",
        "--quiet",
    )

    if mode == FULL_MODE:
        test_cmd += ("--live",)

    commands.extend([
        (uv, "run", "--locked", "coverage", "erase"),
        test_cmd,
        (uv, "run", "--locked", "coverage", "combine"),
        (uv, "run", "--locked", "coverage", "report"),
        (uv, "run", "--locked", "coverage", "html"),
    ])

returncode = 0
for cmd in commands:
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        returncode = 1

sys.exit(returncode)
