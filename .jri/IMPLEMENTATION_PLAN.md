# Implementation Plan

- P0: Replace the default shellout harness with the real Pi TypeScript SDK adapter.
  - Keep the public `HarnessInvocation` / `HarnessResult` boundary as the JRI-owned contract.
  - Make the SDK adapter the production path for interrogator, auditor, planner, builder, and explorer sessions; keep CLI/legacy shellout only as an explicit compatibility or test path.
  - Preserve JRI-controlled model selection, prompt/context refs, capability descriptors, output sink writes, artifact refs, and handoff parsing.
  - Map provider auth, model resolution, missing capability, timeout, cancellation, invalid handoff, and SDK failures into actionable `JriError`/loop failure evidence.
  - Extend fake harness coverage for assistant chunks, artifacts, capability errors, auth errors, delays, cancellation, malformed/missing handoffs, and wrong-agent/wrong-phase handoffs.

- P0: Thread real cancellation through chat, loop phases, harness sessions, and capability processes.
  - Replace fresh disconnected `AbortController().signal` values with lifecycle-owned signals for chat turns and loop runner phases.
  - Honor cancellation before start, during SDK/session execution, during web/explorer capability work, after timeout, and during halt.
  - Use one cancellation path with best-effort termination followed by forceful cleanup after a short grace period.
  - Add tests for pre-start abort, in-flight abort, timeout cleanup, halt while a capability child is active, and no new loop-owned capability work after graceful stop boundaries.

- P0: Enforce daemon-owned lifecycle mutation and race-safe locking.
  - Replace `acquireLock` read/write/reread with a true single-writer guarantee: file lock, real CAS, or daemon-only serialized mutation.
  - Remove or constrain local mutation fallbacks for `loop.requestStop()`, `loop.halt()`, and `loop.resume()` so public lifecycle controls start/use the daemon instead of bypassing it.
  - Keep local fallback behavior only for read-only status/log inspection and tests with explicit fakes.
  - Add contention tests for simultaneous start/resume/stop/halt, stale live locks, stale dead locks, lock loss during runner heartbeat, and daemon unavailable mutation attempts.

- P0: Finish capability ownership, chat-owned capability support, and child registration.
  - Implemented/covered: explicit internal owner metadata as `{ owner: { kind: "loop", loopId } | { kind: "chat", turnId }, projectDir, capability }` and chat-owned web artifacts under `.jri/logs/interrogation-artifacts/`.
  - Ensure chat-owned capabilities cannot mutate loop status or append loop events.
  - Register loop-owned web/explorer child processes with the runner so halt cancels runner plus children and capability timeouts produce structured evidence.
  - Keep first-class web capability usability for auditor, explorer, and interrogator without relying on broad shell access.
  - Cover loop owner mismatch, chat/loop ownership separation, stale owner metadata, explorer spawn-only mode, child cancellation, and capability artifact refs.

- P0: Harden runtime recovery, durable-state validation, and failure evidence.
  - Finding from explorer review: `runControlledPiSession` still appends stdout/stderr concurrently when writing stdout, so output sink serialization belongs with runtime/capability cleanup.
  - Convert malformed/missing handoff parser failures, SDK errors, capability failures, and runner phase mismatches into structured loop failure/recovery events and status updates.
  - Reconcile event/status ordering with the spec where lifecycle transitions currently write status before milestone events.
  - Make halt precedence explicit when stop/natural exit races occur, including final halt/reset outcome.
  - Add coverage for invalid runner phase at runtime, malformed handoff failure evidence, stop/halt races, forceful halt, dead runner repair, and status/event recovery.

- P0: Finish validation and git-safety semantics.
  - Keep builder/Ralph responsible for discovering and running target-project validation from `AGENTS.md` or equivalent project guidance; core records handoff evidence and guards git/tag success.
  - Require at least one concrete passing validation item before accepting git-changing successful iterations.
  - Preserve changed files for inspection on validation failure and blocker outcomes.
  - Ensure no-op success, missing validation evidence, absent validation commands, unsafe validation commands, blocked with unexpected git changes, failed validation with unexpected git changes, and missing/ambiguous tag evidence are all covered.
  - Keep destructive rollback behind explicit halt/reset confirmation across every failure shape.

- P0: Finish remaining human-task resume guardrails.
  - Keep `done` limited to verification and blocker resolution recording; it must not start a runner.
  - Make `jri loop resume` require verified resolution, active loop id, authorized specs fingerprint, and unchanged specs.
  - On resume, consume/clear the blocker and start a fresh runner at the recorded phase.
  - Preserve coverage for changed specs rejection and any remaining resume guardrails not covered by the completed phase-tracking increment.

- P0: Finish auth UX around the real Pi-backed flow.
  - Implement or passthrough real Pi-backed login/logout/status operations beyond local corrupt-cache recovery.
  - Normalize unsupported passthrough errors through JRI auth result types.
  - In interactive bare `jri`, launch or guide inline auth and continue into interrogation after success.
  - In non-interactive mode, print direct recovery commands and exit cleanly without opening a different product mode.
  - Keep core auth UI-neutral; CLI owns display.

- P1: Finish the primary terminal workflow and packaging validation.
  - Decide whether the fallback readline REPL is acceptable for MVP dogfood or whether Pi terminal chat UI primitives must be integrated first.
  - If fallback remains, make its status line, blocked guidance, final result display, and active-loop observation behavior match the Pi-backed TUI requirements.
  - Improve `jri loop attach` with the compact attach header and merged live view expected by the specs.
  - Validate the installed/public `jri` bin path and package metadata, not only `bun src/cli/index.ts`.
  - Harden the timing-sensitive attach test with deterministic readiness if it remains flaky under serial validation.

- P1: Clean up generated operational guidance.
  - Replace the scaffolded `AGENTS.md` placeholders with a better template that tells users how to fill build/run/validation sections without leaving literal `[test command]`-style values as if they were runnable.
  - Update this repository's root `AGENTS.md` Codebase Patterns section with actual patterns useful to Ralph.
  - Decide whether `bun run lint` intentionally aliases `tsc --noEmit` or whether a real lint command should be added before treating lint as distinct validation evidence.

- P1: Dogfood the public MVP path and update docs.
  - After the P0 runtime/CLI behavior above is in place, validate `/home/nico/just-ralph-it-dogfood/gupta-to-web` only through public JRI interfaces: bare `jri`, `jri auth ...`, `jri loop attach|stop|halt|resume`, terminal automation, and JRI-visible logs/specs/status/output.
  - Dogfood success requires deployment at `gupta-to-web.mpujia.justralph.it` plus durable artifacts for interrogation, planning, iterations, blockers, validation, deployment, commits, and tags.
  - Update `README.md` after the dogfood flow works: install/run basics, auth setup, bare `jri` workflow, loop controls, recovery paths, validation behavior, and dogfood workflow.

- Confirmed complete and not re-listed as active implementation work: daemon-owned accepted chat start stream, active-loop observation-mode interrogator restrictions, most fallback CLI chat/event rendering, corrupt-auth cache recovery, core commit/tag guards for successful builder handoffs, malformed `failedValidation` evidence where `passed` is not `false`, verified human-task blocker recording, durable human-task `Blocker.resumePhase` recording, planner/builder blocker resume phase assignment, resume-phase requirement enforcement, planner blockers resuming planning, builder blockers resuming building, duplicate `blockerResolved` prevention, project root resolution, idempotent initialization, manual spec edit reconciliation, and public CLI surface restrictions.
