# Learnings

- `make check` is the canonical repo-wide validation command.
  It runs `ruff check .`, `ruff format --check .`, `ty check`, `python -m jri.checks.schema`, and `pytest` through `uv run`.
- `uv run python -m jri.checks.schema` validates the packaged JSON Schemas, every task file under `.jri/tasks/{draft,todo,doing,done}`, and `.jri/state.json` when it exists.
- Ralph runs now normalize missing outcome markers to an explicit `failed` result with a loud stderr warning; the only canonical runtime outcomes are `completed`, `failed`, and `needs human`.
- A `needs human` outcome is represented durably by a generated `Human` todo task plus a new `depends_on` entry on the original Ralph task.
  Recovery returns the Ralph task to `todo`, writes run context into the Human task body, and lets status/scheduling treat that backlog item as the single source of truth.
