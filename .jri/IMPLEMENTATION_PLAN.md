# Implementation Plan

Planning evidence from this pass:

- Core TypeScript/Bun baseline is in place (`package.json`, `bun.lock`,
  `tsconfig.json`, `src/core`, `src/cli`, Bun scripts).
- `open(projectDir)` and project bootstrap are implemented in `src/core`.
- `ensureInitialized()` creates the durable scaffold (`.jri/config.json`,
  `.jri/status.json`, `.jri/specs`, `.jri/logs`, `.jri/scratchpad.md`, and
  `AGENTS.md`) without generating `.jri/IMPLEMENTATION_PLAN.md`.
- Initialization behavior is covered by tests in
  `tests/core-initialization.test.ts`, including idempotent behavior, malformed
  config handling, and root resolution.

Completed work:

- [x] P0: TypeScript/Bun package baseline with CLI entrypoint, scripts, and
  repository layout (`package.json`, `bun.lock`, `tsconfig.json`, `src/core`,
  `src/cli`).
- [x] P0: Implement public core project API entrypoint (`open`) and
  scaffold initialization path (`lifecycle.ensureInitialized` + idempotent durable
  files).

Next work:

- [ ] P0: Implement core lifecycle/state primitives (atomic status updates,
  lifecycle transitions, lock/heartbeat model, loop metadata, event sequencing).
- [ ] P0: Implement daemon/runtime scaffolding (IPC, process/ownership tracking,
  recovery, idle shutdown).
- [ ] P0: Implement Pi-bounded interrogation/auth/loop paths needed to replace
  placeholder errors in core and CLI.
- [ ] P1: Expand test coverage into status transitions, planner/loop command
  behavior, and daemon recovery paths.
