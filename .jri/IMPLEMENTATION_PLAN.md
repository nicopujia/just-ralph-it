# Implementation Plan

- [ ] Remove the remaining wrapper-path guidance from native capability paths.
  - `buildPiPrompt()` still defaults capability instructions to wrapper commands.
  - `src/core/capabilities.ts` still renders `jri --run-web ...` / `jri --run-explorer ...` guidance.
  - The explorer wrapper prompt still leaks `pi-subagent` / wrapper language.

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
  - `done` / verified human-task resolution updates status but does not emit durable `blockerResolved` evidence.
  - Blocked loop endings do not emit `loopFinished`.
  - Plan-regeneration events are emitted for `needsReplan` but not for the `specsChanged` / `ambiguousSpecsResolved` paths named in the spec.

- [ ] Tighten public contracts and schema validation.
  - `daemonStatus()` and streamed daemon events are not fully validated against the public `ProjectStatus` / `CoreEvent` contracts.
  - Builder validation handoff parsing is looser than the published TypeScript contract.
  - Runtime-state/public-type drift remains around `lastResult.explorer`.
  - Reconcile the spec/implementation mismatch around recovery write ordering and read-path recovery exceptions during implementation.

- [ ] Backfill focused regression coverage for the confirmed gaps.
  - Native chat-level web search/fetch coverage has now been added in this turn; remaining: plain `jri` smoke coverage for native capability paths.
  - Add regressions for capability grant mismatches, blocked-open guide behavior, `done` -> `blockerResolved`, blocked `loopFinished`, plan-regeneration reasons, and daemon payload validation.
