# Implementation Plan

- [ ] Enforce capability policy consistently across prompt, runtime, and tool registration.
  - Web operation declarations are not fully enforced when the capability operation is omitted.
  - Explorer runs are over-granted both web operations regardless of task.
  - The explorer internal entrypoint does not use the same owner-metadata contract as web.

- [ ] Close the remaining bare-`jri` dogfood readiness item.
  - Current state: bare interactive `jri` uses a CLI-owned Pi-backed terminal surface via `@earendil-works/pi-tui`, preserves inline auth, routes turns through `project.chat.send()`, streams assistant output incrementally, and has installed-bin smoke coverage.
  - Remaining: confirm whether the current `@earendil-works/pi-tui` surface is sufficient evidence for the spec's required Pi terminal chat primitives, or record the fallback rationale as durable dogfood evidence.

- [ ] Fix blocked/interrogation lifecycle recovery gaps.
  - Sealed topics do not automatically unseal when accepted changes update them.
  - Opening a blocked project does not automatically show the blocker resolution guide.
  - Blocked/stopped interrogation context omits recent loop events/stdout needed for blocker recovery.
  - Blocked loop endings do not emit `loopFinished`.
  - Plan-regeneration events are emitted for `needsReplan` but not for the `specsChanged` / `ambiguousSpecsResolved` paths named in the spec.
  - Builder handoffs accept interrogation-artifact refs.
  - Capability preflight does not validate default web/explorer implementations.
  - Chat-owned capability ownership is not validated against the active turn.

- [ ] Tighten public contracts and schema validation.
  - `daemonStatus()` and streamed daemon events are not fully validated against the public `ProjectStatus` / `CoreEvent` contracts.
  - Builder validation handoff parsing is looser than the published TypeScript contract.
  - Runtime-state/public-type drift remains around `lastResult.explorer`.
  - Reconcile the spec/implementation mismatch around recovery write ordering and read-path recovery exceptions during implementation.

- [ ] Backfill focused regression coverage for the confirmed gaps.
  - Prompt/harness regressions now cover native default capability prompts and explorer delegation wording.
  - Focused harness coverage now verifies native SDK web execution enforces the same loop-ownership checks as the wrapper path, and the direct `runWebSearch()` / `runWebFetch()` tests now activate a loop fixture explicitly because loop ownership is part of the production contract; remaining: plain `jri` smoke coverage for native capability paths.
  - Add regressions for capability grant mismatches, blocked-open guide behavior, blocked `loopFinished`, plan-regeneration reasons, and daemon payload validation; verified `done` durable `blockerResolved` coverage was added in this turn.
