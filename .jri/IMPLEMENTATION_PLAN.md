# Implementation Plan

- P0: Replace the shellout harness with the real Pi TypeScript SDK adapter.
  - Confirmed gap: production harness still shells out through `Bun.spawn(["pi", ...])` in `src/core/harness.ts`, and `package.json` has no Pi SDK dependency.
  - Add the Pi SDK dependency and make controlled SDK sessions the production path for interrogator, auditor, planner, builder, and explorer.
  - Keep Pi CLI shellout only as an explicit compatibility or test path.
  - Build sessions from JRI-owned auth, model resolution, selected context refs, capability descriptors, output sinks, artifact refs, handoff parsing, and cancellation signals.
  - Enforce clean runtime isolation from ambient Pi packages, skills, MCPs, prompts, settings, memories, sessions, and broad user config.
  - Expand fake adapter coverage for chunks, artifacts, auth/model/capability failures, delays, cancellation, malformed/missing/duplicate/wrong-phase handoffs, raw handoff preservation, and SDK failure normalization.

- P0: Make capabilities first-class SDK/runtime features.
  - Confirmed implemented: web/explorer headline limits, wrapper entrypoints, owner metadata validation for web, chat-owned web artifact isolation, and planner/builder capability declarations exist.
  - Confirmed gap: web/explorer are still exposed mainly as prompt-level `jri --run-web` / `jri --run-explorer` wrapper commands instead of SDK capabilities, so no-bash phases cannot reliably use required web.
  - Confirmed implemented: web operation declarations are now enforced for prompt-rendered `--run-web` metadata and CLI dispatch, with tests covering search-only/fetch-only prompt rendering and mismatched operation rejection.
  - Confirmed implemented: runtime boundary now validates declared capabilities against descriptor `allowedAgents`, chat-vs-loop ownership, phase suitability, and web operation names before default Pi invocation or injected daemon harness adapters.
  - Why this matters: it is now verified at harness invocation, so tests and implementation can assert that undeclared/wrong-owner/wrong-phase operations fail fast instead of silently running through trusted capabilities.
  - Confirmed gap: `pi-web-access` and `pi-subagent` are not bundled or preflighted, while the specs require no manual user package setup for MVP capability use.
  - Confirmed gap: explorer use is mandatory for the dogfood MVP, but runtime can complete without any explorer event or availability proof.
  - Wire web and explorer through declared SDK capabilities and fail undeclared capability use beyond the web wrapper operation checks already in place.
  - Make web usable by no-bash phases, with strict fetch/search result validation and actionable degraded/error outcomes.
  - Make explorer delegation JRI-owned, bundled or preflighted, read-only, spawn/fresh by default, concurrency-limited, handoff-bounded, artifact-backed, and isolated from ambient Pi state.
  - Standardize chat/loop owner metadata for all internal capability entrypoints; `--run-explorer` still accepts positional `projectDir loopId`.

- P0: Finish cancellation, timeout, halt fanout, and runtime recovery invariants.
  - Confirmed gap: halt kills only `status.process.pid`; there is no runner-owned registry for web/explorer/capability children or halt fanout.
  - Confirmed gap: timeout, cancellation, connected-but-silent daemon IPC, capability failures, and malformed handoffs still need consistent durable failure evidence.
  - Confirmed gap: remaining event/status ordering issues include status updates before `iterationStarted` and startup/phase transitions whose exceptions need to be explicit.
  - Confirmed gap: auditor-reported `specsFingerprint` is trusted without daemon recomputation.
  - Confirmed follow-up gap: recovery should prefer the latest durable terminal loop event even when active status still has dead process/stale lock partial ownership, not only when both lock and process are absent.
  - Register loop-owned capability children with the runner and cancel runner plus children on halt or timeout with SIGTERM-then-SIGKILL escalation.
  - Route pre-start aborts, in-flight aborts, timeouts, halt, and graceful-stop boundaries through one cancellation path with structured evidence.
  - Make recovery use the normal single-writer/lock mutation paths where lifecycle state changes.
  - Normalize SDK, capability, parser, phase, lock-loss, cancellation, and timeout failures into structured `loopFinished` failure evidence plus status recovery.

- P0: Harden interrogation readiness and spec mutation safety.
  - Confirmed gap: auditor context does not include unresolved scratchpad scope, though readiness requires unresolved scratchpad questions to be resolved or deferred.
  - Confirmed gap: manual spec reconciliation checks sealed topics but can skip open topics whose spec fingerprints changed.
  - Confirmed gap: recent-turn reconstruction can leak sealed-topic old turns when any topic remains open.
  - Confirmed gap: sealing does not enforce scratchpad cleanup or prove related unresolved notes were moved into specs/deferred.
  - Confirmed gap: bare `jri` blocked-project open does not automatically emit the full resolution guide as a chat/interrogation message.
  - Confirmed gap: observation chat can explain active status but cannot request graceful stop, and planner/builder phases are not guarded against `.jri/specs/*` mutation.
  - Ensure the auditor sees unresolved scratchpad scope and refuses pass until it is resolved into specs or explicitly deferred.
  - Reconcile manual edits for open and sealed topics, filter routine chat context by unsealed/relevant topics, and enforce scratchpad cleanup before sealing.
  - Add observation-mode stop request handling and mutation guards for `.jri/specs/*` during observation, planning, and building.

- P0: Harden handoff validation and persistence.
  - Confirmed implemented: single-line `JRI_HANDOFF_JSON:` extraction, duplicate/missing/wrong-agent/wrong-phase rejection, strict known-key parsing, start-trigger re-verification, validation pass/fail consistency, and git/tag observation.
  - Confirmed implemented: artifact refs are now strict/stable under `.jri/logs/<loopId>/artifacts/*`, and validation artifact refs are preserved on `validationFinished`.
  - Confirmed implemented: auditor handoff failures report `affectedTopics`, `findings`, and follow-up `questions` structurally instead of free-form-only.
  - Confirmed implemented: obvious credential-shaped handoff fields are rejected in parser/runtime validation, with documented detection limits.
  - Confirmed follow-up gap: resume should validate persisted `loopStopped` `nextPhase` and event lineage before trusting resume state.

- P0: Finish minimum CLI/auth control correctness.
  - Confirmed gap: `jri auth login` currently inspects `OPENAI_API_KEY` / Pi auth cache and prints instructions rather than completing or delegating to a real Pi-backed flow.
  - Confirmed gap: interactive bare `jri` exits on `userActionRequired`, while non-interactive mode can proceed until harness auth failure.
  - Confirmed gap: halt reset ineligibility and active-state `loop resume` errors are less actionable than the specs require.
  - Confirmed follow-up gap: CLI auth help advertises advanced passthrough, but unsupported auth subcommands are not actually forwarded.
  - Implement real Pi-backed `jri auth login|logout|status` or normalized passthroughs without requiring raw Pi commands in normal JRI use.
  - Make interactive bare `jri` handle missing auth inline where possible and provide direct recovery in non-interactive mode.
  - Fix reset ineligibility messaging, active-resume guidance, and regression coverage for forbidden public commands/internal entrypoints.

- P1: Finish observation and terminal experience.
  - Confirmed gap: interactive bare `jri` always uses the fallback readline REPL; the project has not decided whether Pi terminal chat primitives can be used with controlled SDK sessions.
  - Confirmed gap: current harness waits for full subprocess output before emitting assistant text.
  - Confirmed gaps: attach lacks compact header/latest milestone view, byte-safe event/stdout cursors, efficient live observe, nonzero synthetic event sequences, and deterministic attach-test readiness.
  - Confirmed follow-up gap: human-task verification through bare `jri` lacks immediate chat/interrogation event visibility until resume.
  - Decide whether Pi terminal chat primitives can be used; otherwise bring fallback readline up to the required status line, blocked guidance, final result display, observation behavior, and streaming expectations.

- P1: Validate public packaging and command surface.
  - Confirmed gap: tests invoke `bun src/cli/index.ts`; `package.json` exposes `jri` directly to a TypeScript source file.
  - Test the installed/public `jri` bin path, not only direct Bun execution.
  - Decide whether the Bun source-file bin is intentional for MVP or whether a built JS/bin wrapper is required.
  - Hide or environment-guard `--daemon`, `--run-loop`, `--run-web`, and `--run-explorer`.
  - Confirmed follow-up gap: CLI/IPC loop command validation matrix is missing unknown loop subcommand and malformed daemon payload cases.

- P1: Decide lint validation semantics.
  - Confirmed gap: `bun run lint` aliases `tsc --noEmit`, so it is not independent lint evidence.
  - Either make `bun run lint` a real lint command or stop documenting it as distinct from typecheck.

- P1: Dogfood the public MVP path and update docs.
  - Run `/home/nico/just-ralph-it-dogfood/gupta-to-web` only through public JRI interfaces.
  - Produce durable evidence for interrogation, specs, planning, explorer use, iterations, blockers if any, validation, deployment, commits, and tags.
  - Verify deployment at `gupta-to-web.mpujia.justralph.it`.
  - Update `README.md` with install/run basics, auth setup, bare `jri` workflow, loop controls, recovery paths, validation behavior, and dogfood workflow.

- P2: Later contract and transport hardening after the MVP loop is usable.
  - Generate JSON Schemas for `status.json`, `interrogation-state.json`, event JSONL, daemon registry, and handoff contracts if TypeScript unions plus parser tests become insufficient.
  - Specify daemon stream cancellation/reconnect behavior beyond the current request/response/event/end protocol if implementation needs it.
  - Add artifact reread-by-ref/range only when a concrete MVP task requires it.
  - Tighten public core exports and remove minor dead code when touching nearby files.
  - Confirmed follow-up gap: daemon request contract hardening should add missing/invalid `projectDir` and malformed `halt` payload cases.
  - Confirmed follow-up gap: capability ownership matrix coverage should include wrong `projectDir`, mismatched `loopId`, stale state, and wrong owner type in one consolidated set of tests.
  - Confirmed follow-up gap: startup lock/runner ownership race paths need focused tests for ownership handoff and lock contention behavior.

- Confirmed complete and not re-listed as active implementation work unless regressions appear:
  - Blocking spec clarifications for dogfood evidence, capability SDK/runtime contracts, artifact refs, validation artifacts, affected auditor topics, `RuntimeStateEvent` / `CoreEvent`, public API boundaries, startup event/status exceptions, and transient file locks.
  - Project root resolution, idempotent initialization, root `AGENTS.md` scaffold cleanup, config/status validation baseline, public omission of documented-forbidden `jri init`, `jri status`, and `jri loop start`.
  - Daemon-owned public lifecycle mutation baseline, read-only local fallback, status mutation locking, loop id generation, basic event sequencing, start-trigger normalization, and runtime recovery for dead processes/stale locks/orphaned active states.
  - Single-line handoff parsing baseline, malformed/duplicate/missing/wrong-agent/wrong-phase rejection, chat trigger daemon start, accepted-trigger gating, manual added/deleted/sealed spec reconciliation baseline, corrupt auth cache recovery, validation-gated git-changing iterations, commit/tag observation, blocker basics, and human-task resume basics.
  - Planner planned handoffs now require `.jri/IMPLEMENTATION_PLAN.md` to exist for initial planning and plan regeneration; missing files produce durable failure evidence instead of `planned` completion.
