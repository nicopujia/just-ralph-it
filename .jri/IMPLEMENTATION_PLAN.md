# Implementation Plan

- P0: Replace the shellout harness with the real Pi TypeScript SDK adapter.
  - Confirmed gap: production harness still builds and spawns `pi --print` subprocess commands in `src/core/harness.ts`, and `package.json` has no Pi SDK dependency.
  - Confirmed gap: prompt/context assembly still reads whole selected files and needs a hard ceiling before SDK session construction.
  - Confirmed gap: the legacy `options.harnessRunner` branch still bypasses harnessAdapter-shaped `owner`, `context`, and `capabilities` payloads, so the compatibility path can skip the same contract the production adapter receives.
  - Keep the public `HarnessInvocation` / `HarnessResult` boundary as the JRI-owned contract and make the SDK adapter the production path for interrogator, auditor, planner, builder, and explorer sessions.
  - Keep subprocess/CLI shellout only as an explicit compatibility or test path, not the default MVP architecture.
  - Preserve JRI-controlled auth, model resolution, selected context refs, capability descriptors, output sink writes, artifact refs, handoff parsing, and cancellation.
  - Confirmed implemented: the default harness preserves raw `JRI_HANDOFF_JSON:` frames for loop-owned output while chat-owned output remains user-visible and stripped, with regression coverage in `tests/harness.test.ts`.
  - Map provider auth, model resolution, missing capability, timeout, cancellation, invalid handoff, and SDK failures into actionable `JriError`s and durable loop failure evidence.
  - Extend fake harness coverage for assistant chunks, artifacts, auth errors, capability errors, delays, cancellation, malformed/missing handoffs, duplicate handoffs, wrong-agent/wrong-phase handoffs, raw handoff preservation, and SDK failure normalization.

- P0: Make capabilities first-class adapter/runtime features instead of prompt-only wrapper commands.
  - Confirmed implemented: chat and loop harness invocations declare web/explorer capabilities, `buildPiPrompt()` can render descriptor-driven capability instructions, web fetch/search limits and artifacts are implemented, chat-owned web artifacts write under `.jri/logs/interrogation-artifacts/`, and explorer spawn mode is concurrency-limited in-process.
  - Confirmed gap: web/explorer are still exposed to agents as `jri --run-web` / `jri --run-explorer` wrapper commands, while read-only phases do not have `bash`, so required web is not reliably usable by interrogator/auditor/explorer agents.
  - Confirmed gap: `allowedToolsForPhase()` still hardcodes tool availability separately from declared capabilities, and capability policy enforcement still leaks through wrapper behavior.
  - Confirmed gap: `runWebSearch()` still accepts structurally malformed result objects by defaulting missing `title`, `url`, `snippet`, and `retrievedAt` fields instead of rejecting the bad JSON shape.
  - Feed explicit JRI capability descriptors through the SDK adapter for interrogator, auditor, planner, builder, and explorer.
  - Ensure web capability is usable by agents that lack broad shell access, and ensure required web failures become actionable capability errors or labeled degraded responses without inventing new `ProjectStatus.blocker.reason` values.
  - Keep explorer delegation JRI-owned, spawn/fresh by default, read-only, concurrency-limited across the active runner, handoff-bounded, and isolated from ambient Pi packages/settings.
  - Decide and encode whether dogfood-scope planner/builder iterations must preflight or prove explorer availability before successful completion; current code declares explorer but does not require it to be used.
  - Make internal capability entrypoints use a common owner metadata contract; `--run-web` already accepts encoded metadata, while `--run-explorer` still accepts positional `projectDir loopId`.
  - Add adapter/fake coverage proving undeclared capabilities fail, declared capabilities are wired, no-bash phases can use web, capability errors are normalized consistently, and dogfood-required explorer unavailability fails loudly.

- P0: Finish lifecycle cancellation, timeout handling, and loop-owned child process registration.
  - Confirmed gap: halt currently kills only `status.process.pid`, and the default killer sends only `SIGTERM`; web and explorer wrappers spawn child processes without a runner-owned child registry or halt fanout.
  - Confirmed gaps: mid-phase cancellation still needs durable evidence for silent stops/timeouts, connected-but-silent daemon IPC needs a timeout path, and loop failure normalization omits capability/cancellation error codes.
  - Confirmed gap: `daemon.shutdown` still answers unconditionally and should refuse while any loop is active instead of shutting the daemon down under load.
  - Register loop-owned web/explorer/capability children with the runner so halt cancels runner plus children, timeout uses the same cancellation path, and captured output/artifacts produce structured evidence.
  - Preserve chat-owned capability isolation under `.jri/logs/interrogation-artifacts/`; chat-owned capabilities must not mutate loop status or append loop events.
  - Honor cancellation before start, during SDK/session execution, during web/explorer capability work, after timeout, during graceful stop boundaries, and during halt.
  - Add tests for active capability halt fanout, SIGTERM-then-SIGKILL escalation, capability timeout evidence, pre-start abort, in-flight abort, halt while a child is active, no new loop-owned capability work after graceful stop boundaries, and daemon request/read timeouts.

- P0: Harden runtime recovery, lifecycle invariants, and event/status consistency.
  - Confirmed implemented: audit failure `blockerReported` parity is covered alongside blocked status, stable handoff path validation rejects traversal-like segments, stop toggle and stopped resume fingerprint gating are implemented, daemon-owned lifecycle mutation is the normal public path, and runtime recovery now repairs active states with no process/lock by consulting the latest loop event when terminal evidence exists or otherwise marking the orphaned active lifecycle as a failed stopped loop with a `statusRepaired` event.
  - Confirmed gaps: recovery checks process/lock liveness but still does not repair startup ownership status when the matching milestone event is missing.
  - Confirmed gaps: auditor-reported `specsFingerprint` is trusted without daemon recomputation, `iterationStarted` writes status before its event, and halt/reset confirmation ordering remains fragile.
  - Normalize malformed/missing handoff parser failures, SDK failures, capability failures, runner phase mismatches, lock loss, and cancellation into structured `loopFinished` failure evidence plus status recovery.
  - Implement the spec's event/status ordering policy, including documented startup/runner ownership exceptions and recovery when ownership status exists without the matching milestone event.
  - Verify auditor fingerprints against core-computed spec fingerprints before storing `authorizedSpecsFingerprint`.
  - Make halt precedence explicit when stop/natural exit races occur, including final halt/reset outcome and idempotent already-halted handling.
  - Add coverage for invalid runner phase at runtime, malformed handoff failure evidence, stop/halt races, forceful halt, dead runner repair with recorded ownership, missing milestone recovery, and status/event recovery.

- P0: Finish Pi-backed auth UX.
  - Confirmed gap: `jri auth login` currently inspects `OPENAI_API_KEY` / Pi auth cache and prints instructions; interactive bare `jri` exits on `userActionRequired`, and non-interactive mode can proceed until a harness operation fails.
  - Implement or passthrough real Pi-backed login/logout/status operations without requiring users to run raw Pi commands in the normal path.
  - Normalize unsupported passthrough errors through JRI auth result types.
  - In interactive bare `jri`, launch or guide inline auth and continue into interrogation after success when possible.
  - In non-interactive mode, print direct recovery commands and exit cleanly when auth is required for the requested operation.
  - Keep core auth UI-neutral; CLI owns display.

- P1: Tighten durable validation, schema, and safety checks.
  - Confirmed implemented: core records validation evidence, rejects git-changing successful handoffs without at least one concrete `passed: true` validation item, rejects successful builder `continue`/`complete` handoffs that include failed validation evidence even without git mutation, records commits/tags it observes, guards blocked/failed-validation outcomes against unexpected git mutations, and requires the expected next semantic-version patch tag on successful git-changing iterations.
  - Confirmed gap: multi-commit iterations are not detected because runtime compares only previous `HEAD` with final `HEAD`.
  - Confirmed gaps: `validateStatus()` validates shape but not all lifecycle invariants, and interrogation-state durable `specFile` validation is looser than the stable `.jri/specs/*` path rules used by handoffs.
  - Preserve changed files for inspection on validation failure and blocker outcomes; keep destructive rollback behind explicit halt/reset confirmation.
  - Add tests for multi-commit iterations, active states without loop ids, idle with active loop ids, blocked without blocker details, and traversal-like interrogation-state spec paths.

- P1: Finish observation-mode and terminal workflow hardening.
  - Confirmed gap: active-loop observation mode is prompt/handoff-restricted, but the interrogator still has write/edit tools and no post-run file-diff guard against `.jri/specs/*` mutation.
  - Confirmed gap: CLI always uses the fallback readline REPL and `invokeDefaultHarness()` waits for full subprocess output before emitting assistant text.
  - Confirmed gaps: attach lacks the compact header/latest milestone view, attach replay ordering still needs byte-safe cursor handling, live observe rereads whole files, and synthetic stdout events use sequence `0`.
  - Decide whether Pi terminal chat UI primitives can be used with JRI-controlled SDK sessions; if yes, integrate them without accepting ambient Pi session history/config.
  - If fallback remains, make it match the required status line, blocked guidance, final result display, active-loop observation behavior, and streaming assistant output expectations.
  - Add observation-mode mutation guards around `.jri/specs/*` and lifecycle files, or run observation with a narrower capability/tool set that makes forbidden mutation impossible.
  - Improve `jri loop attach` with the compact attach header and merged live view expected by the specs, while keeping footer bytes out of `stdout.log`.
  - Harden the timing-sensitive attach test with deterministic readiness.

- P1: Validate packaging and public command behavior.
  - Confirmed gap: tests invoke `bun src/cli/index.ts`; `package.json` exposes `jri` directly to a TypeScript source file.
  - Validate the installed/public `jri` bin path and package metadata, not only direct Bun execution.
  - Decide whether the source-file bin is intentional for Bun-only MVP distribution or whether a built JS/bin wrapper is required.
  - Decide whether internal commands such as `--daemon`, `--run-loop`, `--run-web`, and `--run-explorer` need an environment guard or stronger internal naming for the public package surface.
  - Add smoke coverage for `jri`, `jri auth --help`, and `jri loop ...` through the public bin path.

- P1: Decide lint validation semantics.
  - Confirmed implemented: scaffolded `AGENTS.md` no longer emits literal placeholder validation commands, the primary CLI spec matches that safer template, and this repository's root `AGENTS.md` includes brief codebase patterns.
  - Confirmed gap: `bun run lint` currently aliases `tsc --noEmit`, so it is not independent lint evidence.
  - Decide whether `bun run lint` should become a real lint command or whether validation/docs should stop treating it as distinct evidence.

- P1: Dogfood the public MVP path and update docs.
  - After the P0 runtime/CLI behavior above is in place, validate `/home/nico/just-ralph-it-dogfood/gupta-to-web` only through public JRI interfaces: bare `jri`, `jri auth ...`, `jri loop attach|stop|halt|resume`, terminal automation, and JRI-visible logs/specs/status/output.
  - Dogfood success requires deployment at `gupta-to-web.mpujia.justralph.it` plus durable artifacts for interrogation, planning, iterations, blockers, validation, deployment, commits, and tags.
  - Update `README.md` after the dogfood flow works: install/run basics, auth setup, bare `jri` workflow, loop controls, recovery paths, validation behavior, and dogfood workflow.

- P2: Consider later schema, contract, and transport hardening after the MVP loop is usable.
  - Add generated JSON Schemas for `status.json`, `interrogation-state.json`, event JSONL, daemon registry, and handoff contracts if TypeScript unions plus parser tests become insufficient.
  - Specify daemon stream frame cancellation/reconnect behavior in more detail if implementation needs behavior beyond the current request/response/event/end protocol.
  - Add web artifact reread-by-ref/range only when a concrete MVP task requires it; the spec now marks it post-MVP unless needed.
  - Consider tightening public core exports so callers cannot construct `Project` directly or rely on un-namespaced helper methods outside `open()`.
  - Remove minor dead code such as unused handoff parser helpers when touching nearby files.

- Confirmed complete and not re-listed as active implementation work: deterministic project root resolution, idempotent initialization, config schema validation, public CLI surface restriction for documented commands, no public `jri init` / `jri status` / `jri loop start`, daemon-owned public loop mutation methods, local fallback only for read-only status/observe, status mutation file locking, loop id generation, basic event sequencing, start trigger normalization, accepted chat trigger daemon start stream, manual spec edit and added-spec reconciliation, deleted open-topic and sealed missing-fingerprint start-gate detection, deleted sealed-spec reconciliation with intentional deletion and restore paths, corrupt-auth cache recovery, core commit/tag observation for normal success, malformed `failedValidation` evidence where `passed` is not `false`, verified human-task blocker recording, durable `resumePhase` recording, planner/builder blocker resume phase assignment, resume-phase requirement enforcement, planner blockers resuming planning, builder blockers resuming building, duplicate `blockerResolved` prevention for human-task resume, web/explorer ownership metadata validation, bounded web fetch/search wrappers, chat-owned web artifact isolation, explorer spawn-only wrapper, in-process explorer concurrency queue, legacy non-canonical handoff frame rejection with durable runtime failure evidence, stable `.jri/specs/*` and `.jri/logs/*` handoff path traversal validation, most fallback CLI state errors, AGENTS.md scaffold/root operational guidance cleanup, and resolved stopped-loop/ambiguous-spec guardrails: active auditing may temporarily preserve an ambiguous-spec blocker until audit passes, so the invariant is that blocked status must have blocker details rather than blockers being forbidden outside blocked in all cases, with one `blockerResolved` on pass and continued `needsHumanTask` resume coverage; resolved P1 validation/schema gaps for `validateStatus()` lifecycle invariants and interrogation-state `specFile` path validation.
  - Confirmed complete: human-task verifier coverage for the `path-exists` branch and unsupported-criteria fallback is now covered in `tests/chat.test.ts`.
