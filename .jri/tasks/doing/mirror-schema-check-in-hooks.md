---
title: Mirror schema check in git hooks
priority: 2
assignee: Ralph
depends_on:
  - phase-1-2-quality-gate-foundation
acceptance_criteria:
  - `.pre-commit-config.yaml` contains a new hook entry that runs `uv run python -m jri.checks.schema` with `pass_filenames: false` in the default (pre-commit) stage.
  - The hook is placed alongside the existing `ruff` and `ruff-format` hooks (before `ty` and `pytest`, which are pre-push only).
  - `docs/contrib.md` explicitly states that git hooks have full parity with `make check` — every gate `make check` runs is also covered by a hook.
  - `make check` still passes after the change.
---

`make check` runs lint, format, schema-check, typecheck, and test.
The git hooks cover lint, format (pre-commit), typecheck, and test (pre-push), but skip schema-check.
Close that gap by adding schema-check as a pre-commit hook.

The schema check runs in ~0.5s, so the pre-commit stage is appropriate.
After this change, every gate in `make check` must also be covered by a git hook.
