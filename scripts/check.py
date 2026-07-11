#!/usr/bin/env -S uv run --script

"""Format, lint, and type-check the project."""

import subprocess
from shutil import which


def main() -> None:
    uv = which("uv")
    if not uv:
        raise RuntimeError("uv must be installed")
    for command in (["ruff", "format", "-q"], ["ruff", "check", "--fix", "-q"], ["basedpyright"]):
        subprocess.run([uv, "run", "--locked", *command], check=True)


if __name__ == "__main__":
    main()
