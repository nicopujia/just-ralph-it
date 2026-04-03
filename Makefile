.PHONY: check lint format typecheck schema-check test

check: lint format typecheck schema-check test

lint:
	uv run ruff check .

format:
	uv run ruff format --check .

typecheck:
	uv run ty check

schema-check:
	uv run python -m jri.checks.schema

test:
	uv run pytest
