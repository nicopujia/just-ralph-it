# Implementation Plan

- P0: Replace the shellout harness with the real Pi TypeScript SDK adapter.
  - Confirmed gap: production harness still builds `pi --print` subprocess commands in `src/core/harness.ts`, and `package.json` has no Pi SDK dependency.
  - Confirmed gap: prompt/context assembly still needs a hard ceiling.
  - Keep the public `HarnessInvocation` / `HarnessResult` boundary as the JRI-owned contract and make the SDK adapter the production path for interrogator, auditor, planner, builder, and explorer sessions.
  - Keep subprocess/CLI shellout only as an explicit compatibility or test path, not the default MVP architecture.
  - Preserve JRI-controlled auth, model resolution, selected context refs, capability descriptors, output sink writes, artifact refs, handoff parsing, and cancellation.
  - Map provider auth, model resolution, missing capability, timeout, cancellation, invalid handoff, and SDK failures into actionable `JriError`s and durable loop failure evidence.
  - Extend fake harness coverage for assistant chunks, artifacts, auth errors, capability errors, delays, cancellation, malformed/missing handoffs, wrong-agent/wrong-phase handoffs, and SDK failure normalization.

- P0: Make capabilities first-class adapter/runtime features instead of prompt-only wrapper commands.
  - Confirmed implemented: chat interrogator harness invocations now declare web search/fetch capabilities, and `buildPiPrompt()` can render capability instructions from explicit declared descriptors.
  - Confirmed implemented: tests cover descriptor-driven prompt omission/inclusion, so the prompt path now reflects declared capabilities instead of implicit hardcoded assumptions.
  - Confirmed gap: `allowedToolsForPhase()` still hardcodes tool availability, and web/explorer usage is still exposed to agents as shell commands in the broader runtime path.
  - Confirmed gaps: capability policy enforcement still leaks through wrapper behavior, and cross-process explorer concurrency is not yet bounded.
  - Feed explicit JRI capability descriptors through the harness adapter for interrogator, auditor, planner, builder, and explorer.
  - Ensure web capability is usable by agents that lack broad shell access, and ensure required web failures become actionable capability errors or labeled degraded responses without inventing new `ProjectStatus.blocker.reason` values.
  - Keep explorer delegation JRI-owned, spawn/fresh by default, read-only, concurrency-limited, handoff-bounded, and isolated from ambient Pi packages/settings.
  - Add adapter/fake coverage proving undeclared capabilities fail, declared capabilities are wired, and capability errors are normalized consistently.

- P0: Finish lifecycle cancellation, timeout handling, and loop-owned child process registration.
  - Confirmed gap: halt currently kills only `status.process.pid`; web and explorer wrappers spawn child processes without a runner-owned child registry.
  - Confirmed gaps: mid-phase cancellation still needs durable evidence for silent stops/timeouts, and daemon IPC still needs a timeout path for a connected-but-silent daemon.
  - Register loop-owned web/explorer/capability children with the runner so halt cancels runner plus children, timeout uses the same cancellation path, and captured output/artifacts produce structured evidence.
  - Preserve chat-owned capability isolation under `.jri/logs/interrogation-artifacts/`; chat-owned capabilities must not mutate loop status or append loop events.
  - Honor cancellation before start, during SDK/session execution, during web/explorer capability work, after timeout, during graceful stop boundaries, and during halt.
  - Add tests for active capability halt fanout, capability timeout evidence, pre-start abort, in-flight abort, halt while a child is active, and no new loop-owned capability work after graceful stop boundaries.

- P0: Harden runtime recovery and event/status consistency.
  - Confirmed gap: recovery checks process/lock liveness but does not yet consult latest loop events for stale or missing cross-file state repairs.
  - Confirmed resolved: audit failure `blockerReported` parity is covered alongside blocked status and `auditFailed` evidence.
  - Confirmed resolved: handoff spec/artifact path validation now rejects unstable `.jri/specs/*` and `.jri/logs/*` refs with empty, dot, parent, double-slash, backslash, or NUL segments.
  - Confirmed gaps: auditor fingerprint trust still needs daemon verification, and halt/reset prompt ordering still needs explicit handling.
  - Normalize malformed/missing handoff parser failures, SDK failures, capability failures, runner phase mismatches, and lock loss into structured `loopFinished` failure evidence plus status recovery.
  - Implement the spec's event/status ordering policy, including documented startup/runner ownership exceptions and recovery when ownership status exists without the matching milestone event.
  - Make halt precedence explicit when stop/natural exit races occur, including final halt/reset outcome.
  - Add coverage for invalid runner phase at runtime, malformed handoff failure evidence, stop/halt races, forceful halt, dead runner repair from latest events, and status/event recovery.

- P0: Finish validation and git/tag safety semantics.
  - Confirmed implemented: core records validation evidence, rejects git-changing successful handoffs without at least one concrete `passed: true` validation item, records commits/tags it observes, and guards blocked/failed-validation outcomes against unexpected git mutations.
  - Confirmed resolved: successful git-changing iterations now require the expected next semantic-version patch tag, reject missing tags, ambiguous tags, wrong-commit tags, tag-only mutations, and uncommitted tracked-file mutations after a successful handoff, populate successful `lastResult.validationPassed`, and validate `validation.passed` against `exitCode`.
  - Preserve changed files for inspection on validation failure and blocker outcomes; keep destructive rollback behind explicit halt/reset confirmation.
  - Keep tests focused on safety boundaries because implementation mistakes here can falsely publish unvalidated or incorrectly tagged work; retain coverage for no-op success, missing validation evidence, absent/unsafe validation commands, failed validation with unexpected git changes, blocked with unexpected git changes, expected tag acceptance, missing/ambiguous/wrong-commit tags, tag-only mutations, validation/exit-code mismatch, and uncommitted tracked-file mutations.

- P0: Finish Pi-backed auth UX.
  - Confirmed gap: `jri auth login` currently inspects `OPENAI_API_KEY` / Pi auth cache and prints instructions; interactive bare `jri` exits on `userActionRequired`.
  - Implement or passthrough real Pi-backed login/logout/status operations without requiring users to run raw Pi commands in the normal path.
  - Normalize unsupported passthrough errors through JRI auth result types.
  - In interactive bare `jri`, launch or guide inline auth and continue into interrogation after success when possible.
  - In non-interactive mode, print direct recovery commands and exit cleanly without opening a different product mode.
  - Keep core auth UI-neutral; CLI owns display.

- P1: Finish the primary terminal workflow and attach rendering.
  - Confirmed gap: CLI always uses the fallback readline REPL and `invokeDefaultHarness()` waits for full subprocess output before emitting assistant text.
  - Confirmed gaps: attach replay ordering still needs byte-safe cursor handling, and live observe still rereads whole files instead of incremental deltas.
  - Decide whether Pi terminal chat UI primitives can be used with JRI-controlled SDK sessions; if yes, integrate them without accepting ambient Pi session history/config.
  - If fallback remains, make it match the required status line, blocked guidance, final result display, active-loop observation behavior, and streaming assistant output expectations.
  - Improve `jri loop attach` with the compact attach header and merged live view expected by the specs, while keeping footer bytes out of `stdout.log`.
  - Harden the timing-sensitive attach test with deterministic readiness.

- P1: Validate packaging and public command behavior.
  - Confirmed gap: tests invoke `bun src/cli/index.ts`; `package.json` exposes `jri` directly to a TypeScript source file.
  - Validate the installed/public `jri` bin path and package metadata, not only direct Bun execution.
  - Decide whether the source-file bin is intentional for Bun-only MVP distribution or whether a built JS/bin wrapper is required.
  - Add smoke coverage for `jri`, `jri auth --help`, and `jri loop ...` through the public bin path.

- P1: Decide lint validation semantics.
  - Confirmed implemented: scaffolded `AGENTS.md` no longer emits literal placeholder validation commands, the primary CLI spec matches that safer template, and this repository's root `AGENTS.md` includes brief codebase patterns.
  - Confirmed gap: `bun run lint` currently aliases `tsc --noEmit`, so it is not independent lint evidence.
  - Decide whether `bun run lint` should become a real lint command or whether the validation/docs should stop treating it as distinct evidence.

- P1: Dogfood the public MVP path and update docs.
  - After the P0 runtime/CLI behavior above is in place, validate `/home/nico/just-ralph-it-dogfood/gupta-to-web` only through public JRI interfaces: bare `jri`, `jri auth ...`, `jri loop attach|stop|halt|resume`, terminal automation, and JRI-visible logs/specs/status/output.
  - Dogfood success requires deployment at `gupta-to-web.mpujia.justralph.it` plus durable artifacts for interrogation, planning, iterations, blockers, validation, deployment, commits, and tags.
  - Update `README.md` after the dogfood flow works: install/run basics, auth setup, bare `jri` workflow, loop controls, recovery paths, validation behavior, and dogfood workflow.

- P2: Consider later schema and transport hardening after the MVP loop is usable.
  - Add generated JSON Schemas for `status.json`, `interrogation-state.json`, event JSONL, daemon registry, and handoff contracts if TypeScript unions plus parser tests become insufficient.
  - Specify daemon stream frame cancellation/reconnect behavior in more detail if implementation needs behavior beyond the current request/response/event/end protocol.
  - Add web artifact reread-by-ref/range only when a concrete MVP task requires it; the spec now marks it post-MVP unless needed.
  - Confirmed gap: web artifact reads still assume whole-file rereads instead of a bounded, incremental path.

- Confirmed complete and not re-listed as active implementation work: deterministic project root resolution, idempotent initialization, config schema validation, public CLI surface restriction, no public `jri init` / `jri status` / `jri loop start`, daemon-owned public loop mutation methods, local fallback only for read-only status/observe, status mutation file locking, loop id generation, basic event sequencing, start trigger normalization, accepted chat trigger daemon start stream, active-loop observation-mode interrogator restrictions, manual spec edit and added-spec reconciliation, deleted open-topic and sealed missing-fingerprint start-gate detection, deleted sealed-spec reconciliation with intentional deletion and restore paths, corrupt-auth cache recovery, core commit/tag observation for normal success, malformed `failedValidation` evidence where `passed` is not `false`, verified human-task blocker recording, durable `resumePhase` recording, planner/builder blocker resume phase assignment, resume-phase requirement enforcement, planner blockers resuming planning, builder blockers resuming building, duplicate `blockerResolved` prevention for human-task resume, web/explorer ownership metadata validation, bounded web fetch/search wrappers, explorer spawn-only wrapper, legacy non-canonical handoff frame rejection with durable runtime failure evidence, stable `.jri/specs/*` and `.jri/logs/*` handoff path validation, most fallback CLI state errors, AGENTS.md scaffold/root operational guidance cleanup, and resolved stopped-loop/ambiguous-spec guardrails: preserve ambiguous-spec blockers until audit pass, emit one `blockerResolved` on pass, and keep needsHumanTask resume coverage.
