#!/usr/bin/env -S uv run --script
"""Run the project validation workflow."""

import shutil
import subprocess
import sys

uv = shutil.which("uv")
if uv is None:
    msg = "uv is not available"
    raise RuntimeError(msg)

commands: list[tuple[str, ...]] = [
    (uv, "run", "--locked", "ruff", "format", "-q"),
    (uv, "run", "--locked", "ruff", "check", "--fix", "-q"),
    (uv, "run", "--locked", "basedpyright"),
]

returncode = 0
for cmd in commands:
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        returncode = 1

sys.exit(returncode)
