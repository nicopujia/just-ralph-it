# Learnings

- `make check` is the canonical repo-wide validation command.
  It runs `ruff check .`, `ruff format --check .`, `ty check`, `python -m jri.checks.schema`, and `pytest` through `uv run`.
- `uv run python -m jri.checks.schema` validates the packaged JSON Schemas, every task file under `.jri/tasks/{draft,todo,doing,done}`, and `.jri/state.json` when it exists.
