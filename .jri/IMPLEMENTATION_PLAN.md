# Implementation Plan

- P0: Replace the shellout harness with the real Pi TypeScript SDK adapter.
  - Confirmed gap: `src/core/harness.ts` still builds `pi` command arrays and executes them with `Bun.spawn` in both the legacy session runner and default harness path; `package.json` has no Pi SDK dependency.
  - Add the Pi SDK dependency and make SDK sessions the production path for interrogator, auditor, planner, builder, and explorer.
  - Use the existing `HarnessInvocation`/`HarnessResult` boundary as the adapter contract; fakes and production must share handoff parsing, output sinks, selected context refs, model config, declared capabilities, artifact refs, cancellation, and failure normalization.
  - Stop recomputing model/provider details inside shell command construction; the production adapter must honor `HarnessInvocation.model` and JRI-owned auth/model resolution.
  - Keep Pi CLI shellout only as an explicit compatibility/test path.
  - Enforce clean runtime isolation from ambient Pi packages, skills, MCPs, prompts, settings, memories, sessions, themes, context files, and broad user config.
  - Normalize missing SDK/package/spawn errors, provider auth errors, model errors, invalid handoffs, SDK failures, timeout, and cancellation into actionable JRI errors that durable runtime failure handling can record.
  - Expand fake adapter coverage for chunks, artifacts, auth/model/capability failures, delays, cancellation, malformed/missing/duplicate/wrong-phase handoffs, raw handoff preservation, and SDK failure normalization.

- P0: Make web and explorer first-class SDK/runtime capabilities.
  - Confirmed partial: capability descriptors, prompt instructions, web result validation, child registration, and explorer evidence events exist.
  - Confirmed gap: agent-facing use is still prompt/internal wrapper commands such as `jri --run-web` and `jri --run-explorer`; `src/core/web-capability.ts` invokes `pi-web-access` by name, `src/core/harness.ts` invokes `npm:pi-subagent` by extension name, and neither implementation is bundled or preflighted.
  - Wire web search/fetch and explorer delegation through declared SDK capabilities, including operation-level enforcement for search-only/fetch-only grants.
  - Make required web usable in no-bash phases without raw shell/browser/package workarounds.
  - Bundle or preflight web/explorer capability implementations; missing implementations must fail with actionable `capability-*` evidence before a loop depends on them.
  - Keep explorer JRI-owned, read-only, spawn/fresh by default, concurrency-limited, handoff-bounded, artifact-backed, and isolated from ambient Pi state.
  - Tighten explorer failure evidence so timeout, cancellation, queue failures, wrapper failures, and SDK failures all record `subagentFailed` or equivalent durable evidence when a loop-owned explorer attempt began.
  - Replace raw-output truncation with a JRI-owned structured explorer summary contract before injecting explorer findings into parent context.
  - Use structured owner metadata for explorer capabilities consistently with web; reject missing, stale, wrong-project, wrong-loop, or wrong-owner metadata.

- P0: Harden interrogation readiness and spec mutation safety.
  - Completed planning increment: clarified `.jri/specs/interrogation-readiness.md` so scratchpad clearance is a deterministic start-gate concern with machine-readable evidence, not only auditor judgment.
  - Completed increment: start-gate manual spec reconciliation checks both open and sealed topics against the last interrogator-reconciled fingerprint, and auditing harness context includes `.jri/interrogation-state.json` plus `.jri/scratchpad.md` when present.
  - Completed increment: scratchpad clearance evidence is now recorded on interrogation topics, and start-gate blocks missing or stale scratchpad clearance before audit proceeds.
  - Confirmed gap: recent-turn reconstruction is coarse and includes the last eight turns whenever any topic is open.
  - Confirmed gap: observation, planner, and builder phases rely on prompts/handoffs rather than deterministic before/after `.jri/specs/*` mutation guards.
  - Filter recent turns by relevant unsealed topics instead of injecting the last eight turns globally.
  - Add before/after `.jri/specs/*` mutation guards for observation, planning, and building. Observation must reject any spec filesystem mutation; planning/building may only mutate specs when a spec-blocking contradiction path explicitly allows it and records durable evidence.

- P0: Finish cancellation, timeout, halt fanout, and runtime failure normalization.
  - Completed planning increment: clarified `.jri/specs/runtime-state.md` and `.jri/specs/sdk-runtime-contracts.md` so failed runtime outcomes are durable `loopFinished.failed` plus `stopped`/`lastResult.failed`, and connected-but-silent daemon IPC must time out without duplicating lifecycle mutations.
  - Completed increments: loop-owned harness, explorer, and web subprocesses append child-process records; halt kills registered children; cancellation and runner timeouts fan out SIGTERM then SIGKILL; runner startup ownership/lock-lost failures normalize into durable failed outcomes.
  - Confirmed gap: connected-but-silent daemon IPC, parser failures, broader lock loss, capability failures, and direct stream failures still need a consolidated structured failure audit across every path.
  - Add bounded inactivity timeouts for daemon unary and streaming clients. Read-only status/observe may recover from durable state; ambiguous lifecycle-mutating requests must not be reissued unless core can prove the original request was not accepted.
  - Ensure every runtime failure path records a `loopFinished` failure event plus stopped status/`lastResult` evidence that recovery can trust.
  - Add coverage for parser errors, invalid stream frames, daemon disconnect before done, silent daemon after connect, stale lock during phase switch, and capability cancellation/error propagation.

- P0: Consolidate human-task verification lifecycle ownership.
  - Completed increment: durable human-task verification now goes through the bare `done` path, which reuses the existing resume lifecycle lock while marking a `needsHumanTask` blocker verified.
  - Completed increment: interrogator `humanTaskVerified` handoffs are guidance only and do not mutate blocker state.

- P1: Finish the primary terminal experience.
  - Confirmed gap: bare interactive `jri` uses the fallback readline REPL and prints status only between prompts; it is not a Pi-backed TUI with a live status line.
  - Confirmed gap: active status rendering is richer for `building` than for `auditing` or `planning`; failed stopped loops need user-facing distinction from graceful stopped loops.
  - Confirmed gap: attach readiness remains timing-sensitive and attach does not truly merge sparse event lines by `stdoutOffset`.
  - Decide and document whether the fallback REPL is the MVP terminal UI or whether Pi terminal chat primitives are required before dogfood.
  - Add a live or refreshed status line for fallback REPL, including active phase, stop flag, blocked/failed/stopped guidance, and latest milestone.
  - Add deterministic readiness for attach tests and improve attach/event ordering semantics or narrow the specs if ordered stdout replay plus sparse event rendering is the MVP contract.
  - Ensure blocked-project bare `jri` presents the full guide as an automatic chat message recorded in interrogation history, not only as status text.

- P1: Preserve raw chat handoff evidence and chat artifact refs.
  - Confirmed gap: loop handoffs are preserved in `stdout.log`, but chat/interrogator handoff frames are stripped from visible output and not clearly preserved as raw handoff evidence.
  - Confirmed gap: `ArtifactRef` only models loop-owned `.jri/logs/<loopId>/artifacts/*`, while specs also allow chat-owned `.jri/logs/interrogation-artifacts/*`.
  - Add executable types/parser coverage for chat-owned artifact refs.
  - Preserve raw interrogator handoff output in an appropriate interrogation log/artifact while keeping user-visible chat free of framed JSON.

- P1: Decide lint validation semantics.
  - Confirmed gap: `bun run lint` aliases `tsc --noEmit`, so it is not independent lint evidence.
  - Either make `bun run lint` a real lint command or stop documenting it as distinct from typecheck.

- P1: Dogfood the public MVP path and update docs.
  - Run `/home/nico/just-ralph-it-dogfood/gupta-to-web` only through public JRI interfaces after the P0 SDK/capability/readiness/runtime items are handled enough to avoid relying on private wrappers.
  - Produce durable evidence for interrogation, specs, planning, explorer use, iterations, blockers if any, validation, deployment, commits, and tags.
  - Verify deployment at `gupta-to-web.mpujia.justralph.it`.
  - Update `README.md` with install/run basics, auth setup, bare `jri` workflow, authorization phrases, loop attach/stop/halt/resume, recovery paths, validation behavior, log/artifact locations, and dogfood workflow.

- P1: Resolve remaining implementation-blocking contract details as they arise.
  - Completed planning increment: clarified validation evidence so unavailable-validation explanations may support no-op/non-git-changing outcomes but are not passing validation for git-changing success.
  - Completed planning increment: formalized the canonical `.jri/specs/*.md` fingerprint algorithm in `.jri/specs/runtime-state.md`.
  - Remaining watchlist: whether repeated direct halt should append an additional `loopHalted` event or only render idempotent guidance; whether non-`needsReplan` plan regeneration should be orchestrator-detected or remain builder-requested for MVP; whether public core should export internal web capability helpers.

- P2: Later contract and transport hardening after the MVP loop is usable.
  - Generate JSON Schemas for `status.json`, `interrogation-state.json`, event JSONL, daemon registry, capability metadata, and handoff contracts if TypeScript unions plus parser tests become insufficient.
  - Add artifact reread-by-ref/range only when a concrete MVP task requires it.
  - Tighten public core exports and remove minor dead code when touching nearby files.
  - Add consolidated capability ownership coverage for wrong `projectDir`, mismatched `loopId`, stale state, wrong owner type, stale chat turn, and chat/loop artifact boundary violations.
  - Add focused startup lock/runner ownership race tests for ownership handoff and lock contention behavior.

- Confirmed complete and not re-listed as active implementation work unless regressions appear:
  - `jri auth status` and `jri auth logout` are implemented, including corrupt-cache recovery coverage.
  - Auth help passthrough is implemented and covered.
  - Daemon missing/invalid `projectDir` and malformed `loop.halt` payload cases are implemented and tested.
  - Attach stdout cursors are byte-safe for multibyte output.
  - Raw stopped/halted/final status rendering has been replaced with user-facing next-action guidance.
  - Attach renders a compact header with latest context/milestones.
  - Synthetic `loopOutput` events use nonzero deterministic sequence values.
  - Public command-surface hardening is complete: internal entrypoints require `JRI_INTERNAL_INVOCATION=1`, daemon/runner/harness-controlled spawns set it, direct user invocation is rejected with public MVP command guidance, the package bin path is covered, and CLI tests pass for this unit.
  - Public omission of documented-forbidden `jri init`, `jri status`, and `jri loop start` remains complete.
  - Dogfood MVP successful completion requires durable `subagentFinished` explorer evidence; completion fails without proof, and `loopFinished` plus status `lastResult` success evidence include explorer proof.
  - Web fetch result validation requires wrapper-provided source URL, fetched timestamp, and `markdown`; rejects generic `content`, non-markdown/plain declared formats, non-text content types, and obvious raw HTML before content enters agent context.
  - Single-line handoff parsing, duplicate/missing/wrong-agent/wrong-phase rejection, strict known-key parsing, validation artifact refs, affected auditor topics, and obvious secret-shaped handoff rejection are implemented.
  - Public event type coverage includes canonical `RuntimeStateEvent` export and `CoreEvent` compatibility alias.
  - Daemon-owned public lifecycle mutation baseline, read-only local fallback, status mutation locking, loop id generation, event sequencing baseline, start-trigger normalization, runtime recovery for dead/stale ownership, planner plan existence checks, and stopped-loop resume lineage checks are implemented.
  - Audit pass computes the canonical `.jri/specs/*.md` fingerprint in daemon/core, rejects auditor fingerprint mismatches as durable runtime failures, persists only the core-computed value, and covers non-empty specs directories by using directory `stat` instead of `Bun.file(directory).exists()`.
  - Start-gate manual spec reconciliation detects edits to open topics as well as sealed topics, and auditor harness context sees interrogation state plus scratchpad refs before authorization.
  - Interactive bare `jri` reuses Pi-backed `jri auth login` recovery inline and continues into the fallback REPL when credentials become available; unsupported or non-interactive auth failures still return actionable recovery.

- Planning audit notes:
  - No `src/lib` directory is present.
  - Repository search found no meaningful `TODO`, `FIXME`, skipped tests, `.only`, or declared flaky tests in `src`, `tests`, or `.jri/specs`.
  - README is currently skeletal and should be treated as documentation work, not implementation evidence.
