"""Command-line configuration loading."""

import argparse
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_MIN_QUOTED_VALUE_LENGTH = 2


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
    for key, value in _read_dotenv(cwd / ".env").items():
        env.setdefault(key, value)
    return env


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").lstrip()
        key, value = stripped.split("=", 1)
        if key := key.strip():
            values[key] = _strip_optional_quotes(value.strip())
    return values


def _strip_optional_quotes(value: str) -> str:
    if len(value) < _MIN_QUOTED_VALUE_LENGTH:
        return value
    if value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
