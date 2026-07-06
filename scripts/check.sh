#!/usr/bin/env bash
set -e
uv run --locked ruff format -q
uv run --locked ruff check --fix -q
uv run --locked basedpyright
