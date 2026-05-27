# Implementation Plan

- P0: Replace the shellout harness with the real Pi TypeScript SDK adapter.
  - Confirmed gap: `src/core/harness.ts` still builds `pi` command arrays and executes them with `Bun.spawn` in both the legacy session runner and default harness path; `package.json` has no Pi SDK dependency.
  - Add the Pi SDK dependency and make SDK sessions the production path for interrogator, auditor, planner, builder, and explorer.
  - Keep Pi CLI shellout only as an explicit compatibility/test path.
  - Build sessions from JRI-owned auth, model resolution, selected context refs, capability descriptors, output sinks, artifact refs, handoff parsing, and cancellation signals.
  - Enforce clean runtime isolation from ambient Pi packages, skills, MCPs, prompts, settings, memories, sessions, and broad user config.
  - Expand fake adapter coverage for chunks, artifacts, auth/model/capability failures, delays, cancellation, malformed/missing/duplicate/wrong-phase handoffs, raw handoff preservation, and SDK failure normalization.

- P0: Make web and explorer first-class SDK/runtime capabilities.
  - Confirmed partial: capability descriptors and boundary validation exist, but agent-facing use is still prompt/internal wrapper commands such as `jri --run-web` and `jri --run-explorer`.
  - Confirmed gap: `src/core/web-capability.ts` invokes `pi-web-access` by name, `src/core/harness.ts` invokes `npm:pi-subagent` by extension name, and neither implementation is bundled or preflighted.
  - Wire web search/fetch and explorer delegation through declared SDK capabilities, including operation-level enforcement for search-only/fetch-only grants.
  - Make required web usable in no-bash phases without raw shell/browser/package workarounds.
  - Bundle or preflight `pi-web-access` and `pi-subagent`; missing implementations must fail with actionable `capability-*` evidence before a loop depends on them.
  - Keep explorer JRI-owned, read-only, spawn/fresh by default, concurrency-limited, handoff-bounded, artifact-backed, and isolated from ambient Pi state.

- P0: Finish cancellation, timeout, halt fanout, and runtime failure normalization.
  - Confirmed gap: `haltProcess` kills only `status.process.pid`; there is no loop-owned child process registry or fanout for web, explorer, or other capability children.
  - Register loop-owned capability children with the runner and cancel the runner plus all registered children on halt or timeout, with SIGTERM-then-SIGKILL escalation.
  - Route pre-start aborts, in-flight aborts, timeouts, halt, graceful-stop boundaries, connected-but-silent daemon IPC, parser failures, lock loss, and capability failures through one structured failure path.
  - Normalize failures into durable `loopFinished` failure events plus stopped status/`lastResult` evidence that recovery can trust.

- P0: Harden interrogation readiness and spec mutation safety.
  - Confirmed gap: auditor context includes specs but not unresolved scratchpad scope; loop context ignores `.jri/scratchpad.md` even when readiness depends on resolving or deferring it.
  - Confirmed gap: manual spec reconciliation checks sealed topics but skips changed open topics; recent-turn reconstruction is coarse and includes the last eight turns whenever any topic is open.
  - Confirmed gap: sealing records `sealedSpecFiles` without proof that related scratchpad notes were cleaned up or moved into specs/deferred scope.
  - Confirmed gap: observation chat can suggest `jri loop stop` but cannot request a graceful stop, and planner/build phases rely on prompts rather than deterministic `.jri/specs/*` mutation guards.
  - Make unresolved scratchpad scope visible to the auditor and block pass until it is resolved into specs or explicitly deferred.
  - Reconcile manual edits for open and sealed topics, filter recent turns by relevant unsealed topics, and enforce scratchpad cleanup proof before sealing.
  - Add observation-mode graceful stop handling and before/after `.jri/specs/*` mutation guards for observation, planning, and building.

- P0: Fix remaining public CLI/auth lifecycle correctness.
  - Confirmed gap: interactive bare `jri` exits with status 1 on `userActionRequired` instead of continuing an inline auth flow or returning to the REPL with direct recovery.
  - Confirmed partial: fallback status output shows the full blocked resolution guide, but automatic chat/interrogation messages still summarize only the first step.
  - Continue Pi-backed auth recovery inline where possible and emit the full blocked-project resolution guide as an automatic chat/interrogation message.

- P1: Finish attach and fallback terminal experience.
  - Confirmed gap: attach readiness remains timing-sensitive instead of deterministic.
  - Confirmed gap: interactive bare `jri` uses the fallback readline REPL and prints status only between prompts, not as a live status line.
  - Add deterministic readiness for attach tests and a live status line for the fallback REPL.

- P1: Validate public packaging and command surface.
  - Confirmed gap: `package.json` exposes `jri` directly as `./src/cli/index.ts`, while tests invoke `bun src/cli/index.ts`; the installed/public bin path is untested.
  - Confirmed gap: internal entrypoints `--daemon`, `--run-loop`, `--run-web`, `--run-explorer`, and legacy `--web-search`/`--web-fetch` are hidden from usage but still callable if invoked directly.
  - Test the installed/public `jri` bin path, decide whether the TypeScript source-file bin is intentional for MVP, and hide or environment-guard internal entrypoints.

- P1: Decide lint validation semantics.
  - Confirmed gap: `bun run lint` aliases `tsc --noEmit`, so it is not independent lint evidence.
  - Either make `bun run lint` a real lint command or stop documenting it as distinct from typecheck.

- P1: Dogfood the public MVP path and update docs.
  - Run `/home/nico/just-ralph-it-dogfood/gupta-to-web` only through public JRI interfaces.
  - Produce durable evidence for interrogation, specs, planning, explorer use, iterations, blockers if any, validation, deployment, commits, and tags.
  - Verify deployment at `gupta-to-web.mpujia.justralph.it`.
  - Update `README.md` with install/run basics, auth setup, bare `jri` workflow, loop controls, recovery paths, validation behavior, and dogfood workflow.

- P1: Resolve only implementation-blocking spec ambiguities.
  - Confirmed inventory: validation policy has tension between weaker evidence and concrete passing validation for git-changing success; failure final-status semantics need precision; the exact specs fingerprint algorithm is underspecified; daemon stream/error semantics and handoff JSON shape ownership may need tighter wording.
  - Prefer implementation-plan notes until coding would diverge; if needed, make the smallest `.jri/specs/*.md` clarification before implementing the affected area.

- P2: Later contract and transport hardening after the MVP loop is usable.
  - Generate JSON Schemas for `status.json`, `interrogation-state.json`, event JSONL, daemon registry, and handoff contracts if TypeScript unions plus parser tests become insufficient.
  - Specify daemon stream cancellation/reconnect behavior beyond the current request/response/event/end protocol if implementation needs it.
  - Add artifact reread-by-ref/range only when a concrete MVP task requires it.
  - Tighten public core exports and remove minor dead code when touching nearby files.
  - Add consolidated capability ownership coverage for wrong `projectDir`, mismatched `loopId`, stale state, and wrong owner type.
  - Add focused startup lock/runner ownership race tests for ownership handoff and lock contention behavior.

- Confirmed complete and not re-listed as active implementation work unless regressions appear:
  - `jri auth status` and `jri auth logout` are implemented, including corrupt-cache recovery coverage.
  - Auth help passthrough is implemented and covered.
  - Daemon missing/invalid `projectDir` and malformed `loop.halt` payload cases are implemented and tested.
  - Attach stdout cursors are byte-safe for multibyte output.
  - Raw stopped/halted/final status rendering has been replaced with user-facing next-action guidance.
  - Attach now renders a compact header with latest context/milestones.
  - Synthetic `loopOutput` events now use nonzero deterministic sequence values.
  - Bare blocked status formatting includes the full resolution guide; only automatic chat/interrogation blocked guidance remains active above.
  - Public omission of documented-forbidden `jri init`, `jri status`, and `jri loop start` remains complete; internal entrypoint guarding remains active above.
  - Dogfood MVP successful completion now requires durable `subagentFinished` explorer evidence; completion fails without proof, and `loopFinished` plus status `lastResult` success evidence include the explorer proof. Broader first-class SDK/runtime capability work for explorer and web remains active above.
  - Runtime failure normalization now treats `harness-cancelled`, `runtime-cancelled`, `explorer-failed`, `capability-*`, and `web-capability-*` errors as durable loop failures; child-process cancellation fanout remains active above.
  - Web fetch result validation now requires wrapper-provided source URL, fetched timestamp, and `markdown`; rejects generic `content`, non-markdown/plain declared formats, non-text content types, and obvious raw HTML before content enters agent context. This matters because fetched web content is injected into agent context, so core must not invent provenance or pass raw HTML as markdown. Covered by `tests/harness.test.ts`.
  - Single-line handoff parsing, duplicate/missing/wrong-agent/wrong-phase rejection, strict known-key parsing, validation artifact refs, affected auditor topics, and obvious secret-shaped handoff rejection are implemented.
  - Public event type coverage now includes the canonical `RuntimeStateEvent` export and the `CoreEvent` compatibility alias.
  - Daemon-owned public lifecycle mutation baseline, read-only local fallback, status mutation locking, loop id generation, event sequencing baseline, start-trigger normalization, runtime recovery for dead/stale ownership, planner plan existence checks, and stopped-loop resume lineage checks are implemented.
  - Audit pass now computes the canonical `.jri/specs/*.md` fingerprint in daemon/core, rejects auditor fingerprint mismatches as durable runtime failures, persists only the core-computed value, and covers non-empty specs directories by using directory `stat` instead of `Bun.file(directory).exists()`.
