#!/usr/bin/env -S uv run --script
"""Run the project validation workflow."""

import argparse
import shutil
import subprocess
import sys
from typing import cast

FULL_MODE = "full"
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
    choices=(FULL_MODE, FAST_MODE),
    default=FULL_MODE,
    help="Choose whether to include the test suite.",
)
mode = cast("str", parser.parse_args().mode)

commands: list[tuple[str, ...]] = [
    (uv, "run", "--locked", "ruff", "format", "-q"),
    (uv, "run", "--locked", "ruff", "check", "--fix", "-q"),
    (uv, "run", "--locked", "basedpyright"),
]
if mode == FULL_MODE:
    commands.extend([
        (uv, "run", "--locked", "coverage", "erase"),
        (uv, "run", "--locked", "coverage", "run", "-m", "pytest", "--quiet"),
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
