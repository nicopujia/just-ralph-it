---
title: Add canonical make check quality gate
priority: 0
assignee: Ralph
depends_on: []
acceptance_criteria:
  - A root Makefile exists and exposes a `check` target.
  - `make check` runs the project's agreed quality gates for Phase I and II work.
  - `make check` exits non-zero on any lint, format, type, schema, or test failure.
  - Contributor documentation references `make check` as the canonical pre-merge validation command.
---

Create the canonical quality-gate entrypoint required for the remaining Phase I and II work.

This task should not add new product behavior beyond project scaffolding and validation wiring.
It should consolidate the existing lint, format, typing, schema-validation, and test checks behind `make check` so later tasks can rely on one deterministic repo-wide command.

If any existing check is redundant, document the chosen command set and keep the final surface lean.
