#!/usr/bin/env -S uv run --script

"""Build, compile, format, lint, type-check, and test the project."""

import shutil
import subprocess
from pathlib import Path


def main() -> None:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv must be installed")
    build_path = Path(__file__).parent.parent / ".dist"
    if build_path.exists():
        shutil.rmtree(build_path)
    subprocess.run([uv, "build", "--no-sources", "--out-dir", build_path], check=True)
    for command in (
        ["ruff", "format", "-q"],
        ["ruff", "check", "--fix", "-q"],
        ["python", "-m", "compileall", "-q", "src"],
        ["basedpyright"],
        ["pytest", "-q"],
    ):
        subprocess.run([uv, "run", "--locked", *command], check=True)


if __name__ == "__main__":
    main()
