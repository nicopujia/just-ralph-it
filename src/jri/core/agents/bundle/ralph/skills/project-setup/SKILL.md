---
name: project-setup
description: Use for project setup, greenfield setup, skeleton work, and canonical quality gates.
---

## Project Setup

- Inspect the repo's existing tooling first.
- Use the project's native config and entrypoints.
- If the repo has `make check`, wire it as the canonical quality gate when appropriate.
- Include the applicable formatting, linting, type checking, tests, build, and schema checks.
- Treat local diagnostics tools such as LSPs and specialized analyzers as useful but optional unless the task explicitly requires them.
- If optional diagnostics are unavailable, record the unavailable tool and run the strongest project-native substitutes available, such as `make check`, lint, typecheck, build, tests, schema checks, or a small driver.
- Missing optional diagnostics is not fatal by itself when substitute project gates pass; failing substitute gates remain failures.
- Do not require installing optional diagnostics tooling just to prove the task, and do not hide unavailable tooling from the final evidence.
- Do not write tests for docs, prompts, or config-only changes.
- Keep generated JRI runtime artifacts out of broad scans via the target project's native ignore mechanisms.
- Exclude: `.jri/logs/`, `.jri/signals/`, `.jri/worktree/`, `.jri/*state.json*`, `.jri/metrics.json`.
- Do not blindly exclude durable `.jri/tasks/`, `.jri/attempts/`, or `.jri/learnings.md`; only do so when a tool-specific failure requires it and the reason is documented.

## Shipped And User-Facing QA

- For shipped or user-facing work, especially public MVPs, require concrete interaction QA evidence in addition to automated checks.
- Exercise the happy path, feedback states, repeat/new flow, disabled or invalid actions, refresh/reconnect behavior or a documented graceful limitation, mobile/responsive behavior when relevant, and console/network cleanliness for browser surfaces.
- Prefer Playwright or browser QA for web apps when available.
- For CLI, TUI, API, SDK, service, or hosted-public work, use the matching surface instead: run commands, drive the TUI, call live endpoints, execute a small client/driver, or inspect the public service behavior.
- Capture bounded evidence such as commands, URLs, screenshots, request/response summaries, console/network status, and public-surface/security observations.
- Never capture or paste secrets, raw provider logs, environment dumps, certificates, private keys, tokens, or credentials as QA evidence.
