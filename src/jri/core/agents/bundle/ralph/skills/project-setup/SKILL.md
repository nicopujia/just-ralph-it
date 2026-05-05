---
name: project-setup
description: Use for project setup, greenfield setup, skeleton work, and canonical quality gates.
---

## Project Setup

- Inspect the repo's existing tooling first.
- Use the project's native config and entrypoints.
- If the repo has `make check`, wire it as the canonical quality gate when appropriate.
- Include the applicable formatting, linting, type checking, tests, build, and schema checks.
- Do not write tests for docs, prompts, or config-only changes.
- Keep generated JRI runtime artifacts out of broad scans via the target project's native ignore mechanisms.
- Exclude: `.jri/logs/`, `.jri/signals/`, `.jri/worktree/`, `.jri/*state.json*`, `.jri/metrics.json`.
- Do not blindly exclude durable `.jri/tasks/`, `.jri/attempts/`, or `.jri/learnings.md`; only do so when a tool-specific failure requires it and the reason is documented.
