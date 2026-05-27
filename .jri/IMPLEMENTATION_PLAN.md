# Implementation Plan

Completed work:

- [x] P0: TypeScript/Bun package baseline with CLI entrypoint, scripts, and
  repository layout.
- [x] P0: Implement public core project API entrypoint (`open`) and
  scaffold initialization path.
- [x] P0: Implement core lifecycle/state primitives (runtime-state transitions,
  locking, stale-lock handling, event sequencing, and replay-safe loop control).
- [x] P0: Add daemon/runtime scaffolding and loop observation/halt scaffolding.
- [x] P0: Implement daemon IPC + runner (`status/observe/stop/halt/resume`) and
  readonly fallback behavior.
- [x] P0: Implement Pi-backed execution/session startup with runner process
  ownership and status updates.
- [x] P0: Implement idle shutdown hardening. Daemon idle shutdown now checks
  registered project statuses from `.jri/status.json` and only exits when there are
  no connected clients and no active loops, preventing active Ralph loops from
  losing daemon management.
- [x] P0: Honor `status.stopRequested` in `runLoopProcess` at safe planning and build
  iteration boundaries for graceful stopping, with coverage for both boundary cases.

Next work:

- [ ] P0: Implement full resume runner.
  Remaining work: richer orchestration decisions after Pi returns, commit/tag
  observation, stop-boundary handling, blocker parsing/recording from builder
  output, and eventual SDK-native session wiring.
- [ ] P1: Expand test coverage into status transitions, planner/loop command
  behavior, and daemon recovery paths.

Validation:

- `bun run test`, `bun run typecheck`, and `bun run lint` all pass.
