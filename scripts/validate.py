#!/usr/bin/env -S uv run --script
"""Run the project validation workflow."""

import shutil
import subprocess
import sys

uv = shutil.which("uv")
if uv is None:
    msg = "uv is not available"
    raise RuntimeError(msg)

commands = [
    (uv, "run", *line.split())
    for line in """
ruff format -q
ruff check --fix -q
basedpyright
coverage erase
coverage run --branch -m pytest -q
coverage combine
coverage report --skip-covered --skip-empty
coverage html -q
""".strip("\n").splitlines()
]

returncode = 0
for cmd in commands:
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        returncode = 1

sys.exit(returncode)
