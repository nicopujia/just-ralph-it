#!/usr/bin/env -S uv run --script

"""Build, compile, format, lint, type-check, and test the project."""

import shutil
import subprocess
import tomllib
from pathlib import Path


def main() -> None:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv must be installed")
    root = Path(__file__).parent.parent
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if f'__version__ = "{version}"' not in (root / "src" / "jri" / "__init__.py").read_text():
        raise RuntimeError(f"jri.__version__ must be {version}, as pyproject.toml says")
    build_path = root / ".dist"
    if build_path.exists():
        shutil.rmtree(build_path)
    subprocess.run([uv, "build", "--no-sources", "--out-dir", build_path], check=True)
    for command in (
        ["ruff", "format", "-q"],
        ["ruff", "check", "--fix", "-q"],
        ["python", "-m", "compileall", "-q", "--invalidation-mode", "checked-hash", "src"],
        ["basedpyright"],
        ["pytest", "-q", "--cov=src/jri/core", "--cov=src/jri/lib", "--cov-report=term-missing", "--cov-fail-under=80"],
    ):
        subprocess.run([uv, "run", "--locked", *command], check=True)


if __name__ == "__main__":
    main()
