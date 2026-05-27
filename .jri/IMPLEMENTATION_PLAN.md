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

- [x] P0 (selected): Implement core lifecycle/state primitives.
  Completed via runtime-state primitives with atomic status reads/writes,
  legal lifecycle transition enforcement, lock acquire/heartbeat/release with
  stale-lock checks, UTC loop-id collision handling, canonical `CoreEvent`
  union expansion, and monotonic JSONL event sequence allocation.
- [x] P0 (selected): Add daemon/runtime scaffolding to core loop controls without Pi
  SDK execution yet: recovery-aware status reads, loop event replay observation,
  graceful stop toggle, and halt scaffolding are implemented and tested.
- [x] P0: Implement actual IPC + daemon runner.
  Completed a core-owned JSON-line socket/named-pipe protocol with hidden `--daemon`
  CLI entrypoint, user-state registry, daemon `status/observe/stop/halt/resume`
  routing, read-only fallback for status+observe, lazy startup for mutating loop
  controls, and validation coverage in `tests/daemon-ipc.test.ts`.
- [x] P0: Implement Pi-backed execution/session startup.
  Completed a core-owned runner startup path for eligible resumes: stopped or
  verified human-task loops now start a hidden runner process, record process
  ownership/lock state, emit `loopStarted`, choose planning vs building from the
  presence of `.jri/IMPLEMENTATION_PLAN.md`, run isolated Pi CLI sessions with
  core-owned prompts/model defaults, tee stdout/stderr to
  `.jri/logs/<loopId>/stdout.log`, and complete/fail status from runner exit.
  Tests use an injected runner and fake Pi executable so startup and process
  accounting are covered without live provider calls.
- [ ] P0: Implement idle shutdown hardening (beyond baseline).
- [ ] P0: Implement full resume runner.
  Remaining work: richer orchestration decisions after Pi returns, commit/tag
  observation, stop-boundary handling inside multi-iteration runs, blocker
  parsing/recording from builder output, and eventual SDK-native session wiring.
- [ ] P1: Expand test coverage into status transitions, planner/loop command
  behavior, and daemon recovery paths.
