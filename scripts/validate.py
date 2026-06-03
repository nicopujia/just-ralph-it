#!/usr/bin/env uv run
"""Run the project validation workflow."""

import subprocess

COMMANDS: tuple[tuple[str, ...], ...] = (
    # Formatting
    ("uv", "run", "ruff", "format", "-q"),
    ("uv", "run", "ruff", "check", "--fix", "-q"),
    # Type checking
    ("uv", "run", "basedpyright"),
    # Testing
    ("uv", "run", "coverage", "run", "-m", "pytest", "-q"),
    ("uv", "run", "coverage", "combine"),
    ("uv", "run", "coverage", "report", "--skip-covered", "--skip-empty"),
    ("uv", "run", "coverage", "html", "-q"),
)


def main() -> None:
    """Run validation commands in order."""
    for command in COMMANDS:
        _result = subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
