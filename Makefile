.PHONY: setup check lint format typecheck typecheck-ts schema-check test

setup:
	uv sync --all-groups
	bun install
	./.venv/bin/python -m pre_commit install
	./.venv/bin/python -m pre_commit install --hook-type pre-push

check: lint format schema-check typecheck test

lint:
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --select I --fix .

typecheck: typecheck-py typecheck-ts

typecheck-py:
	uv run basedpyright

typecheck-ts:
	bun run typecheck

schema-check:
	uv run python -m jri.checks.schema

test:
	PYTHONPATH=$(PWD)/src uv run coverage run -m pytest
	PYTHONPATH=$(PWD)/src uv run coverage report
