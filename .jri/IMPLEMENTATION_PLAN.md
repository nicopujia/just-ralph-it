# Implementation Plan

- P0: Replace shellout harness paths with the real `HarnessInvocation` adapter for every agent.
  - Implement the JRI-owned Pi TypeScript SDK adapter for `interrogator`, `auditor`, `planner`, `builder`, and `explorer`; Pi package flags/session details stay inside the adapter.
  - Remove loop-phase dependence on `HarnessSessionRunner`/`runControlledPiSession`; `runLoopProcess` must invoke the adapter with `owner`, `projectDir`, `agent`, `phase`, `model`, `context`, declared `capabilities`, ordered `output`, and `signal`.
  - Make production and fake harnesses share the same `HarnessInvocation`/`HarnessResult` contract, including scripted chunks, handoffs, artifacts, capability errors, auth errors, delays, and cancellation.
  - Map auth, model, capability, invalid-handoff, timeout, cancellation, and SDK failures into structured JRI errors/status events.

- P0: Enforce harness invocation fields, current-message correctness, and cancellation.
  - Fix interrogator prompt construction so `Current user message:` is always the current trimmed user message, not the last inline context item when recent turns are appended.
  - Thread `AbortSignal`/timeout through chat, loop phases, Pi SDK sessions, and capability processes; honor cancellation before start, during execution, and after timeout with best-effort then forceful cleanup.
  - Stop parsing handoffs from shared `stdout.log`; capture each adapter result directly and persist raw display output separately.
  - Add assertions/tests for `owner`, `agent`, `phase`, `model`, selected context refs, capabilities, output sink ordering, and cancelled invocations.
  - Completed/Tested: Default interrogator harness now uses the first inline context entry as the current user message rather than recent-turn context; focused `tests/harness.test.ts` harness tests passed. Final serial validation passed with `bun run test` (119 tests), `bun run typecheck`, and `bun run lint`.

- P0: Restore daemon-owned chat start semantics.
  - Completed/Tested: `sendChat` accepted triggers now default to `daemonStartLoop` streaming instead of the local `startRalphLoop` fallback, while injected `startLoop` remains the explicit test fake path. Focused validation passed with `bun test tests/chat.test.ts` (20 tests). Final serial validation passed with `bun run test` (120 tests), `bun run typecheck`, and `bun run lint`.
  - On accepted trigger, stream `loopStarted` plus subsequent audit/planning/build/blocker/stop/halt/failure/completion events without requiring the caller to separately attach.
  - Reject active loops, unresolved human-task blockers, pending reconciliation, and invalid triggers with state-specific actionable errors.

- P0: Implement active-loop observation-mode interrogator.
  - Active-loop chat short-circuit is resolved: chat now invokes the interrogator with observation-mode context/restrictions, includes status/plan/log refs, and rejects lifecycle-changing handoffs in observation mode. Validation passed: focused `tests/chat.test.ts`, `bun run test`, `bun run typecheck`, and `bun run lint`.
  - Bare `jri`/`chat.send()` must still invoke the Pi-backed interrogator when status is `auditing`, `planning`, or `building`, but with observation-mode context and restrictions.
  - Observation mode may explain status/logs/specs/plan, record thoughts to `.jri/scratchpad.md`, and offer/request graceful stop.
  - Observation mode must not mutate `.jri/specs/*`, trigger replanning, authorize a new lifecycle, or change active requirements.
  - If the user wants requirement changes during an active loop, record the thought in scratchpad and guide them through graceful stop before normal interrogation resumes.

- P0: Finish CLI chat/event rendering for the MVP terminal workflow.
  - Completed/Tested: fallback bare-`jri` rendering now routes `chat.send()` events through a shared renderer for piped and interactive modes; renders `specsUpdated`/`scratchpadUpdated` and normalized loop events beyond `chatMessageDelta`/`loopStarted`; handles `loopOutput` text payloads; and surfaces idle `lastResult` details including URL, validation, commit, and tag. Focused validation passed with `bun test tests/cli.test.ts`.
  - Replace or harden the readline fallback so bare `jri` renders all `chat.send()` events, not only `chatMessageDelta` and `loopStarted`.
  - Render normalized loop lifecycle events from accepted-trigger streams: audit, planning, iteration, validation, commit/tag, blocker, stop, halt, failure, completion, and loop output.
  - Keep stable status/footer behavior with attach/stop guidance; surface final `lastResult` details including URL/deployment, validation result, commit, tag, artifact/log hints, and next action.
  - Validate the installed/public `jri` bin path and packaging, not only `bun src/cli/index.ts`.

- P0: Enforce capability ownership, cancellation, and stdout policy.
  - Add owner metadata validation to internal `--run-web`, `--run-explorer`, and any adapter-only capability entrypoints; reject missing, stale, or mismatched `{ projectDir, loopId, owner }`.
  - Completed/Tested: Internal `--run-web`/`--run-explorer` now validate status-derived loop ownership before executing, and focused CLI tests cover stale and inactive loop rejection.
  - Ensure chat-owned capability artifacts go under `.jri/logs/interrogation-artifacts/` and cannot mutate loop status or append loop events.
  - Register loop-owned web/explorer children with the runner so halt cancels the runner and children; graceful stop prevents new loop-owned capability work only at safe phase/iteration boundaries.
  - Replace concurrent stdout/stderr appends with one ordered merged `stdout.log` writer per loop; record channel-specific evidence in structured events, handoffs, or artifacts when needed.
  - Add explicit tests for explorer spawn-only mode, ownership mismatch, child cancellation, timeout cancellation, and chat/loop ownership separation.

- P0: Finish web capability surfacing and robustness.
  - Surface the JRI web capability through `HarnessInvocation.capabilities` and prompts for every allowed agent, including the interrogator; required web access must fail with an actionable capability blocker or labeled degraded answer, never guessed facts.
  - Implement real process timeouts for `pi-web-access` and route timeout cleanup through the same cancellation path as halt.
  - Fix fetch excerpt/artifact truncation to respect Unicode boundaries and report accurate omitted byte/character counts.
  - Preserve source metadata and artifact refs in capability results without injecting raw oversized HTML/markdown into agent context.
  - Cover search/fetch success, unavailable command, invalid JSON, timeout, redirects/limits, Unicode truncation, and artifact creation.

- P0: Harden runtime mutation, locking, halt, and stopped-start policy.
  - Validate existing `.jri/interrogation-state.json` during `open()`/startup, matching config/status validation; malformed durable interrogation state should fail with an actionable recovery path before chat/start workflows use it.
  - Replace `acquireLock` read/write/reread with a race-safe CAS/status mutation strategy, file lock, or daemon-only single-writer guarantee; add contention tests.
  - Make resolving, resuming, halting, repairing, and starting acquire/check lifecycle ownership consistently, including stale lock ownership and lock-loss paths.
  - Make halt take precedence while the process is live, cancel registered children, escalate from graceful termination to forceful kill after a short grace period, and record the final halt/reset outcome.
  - Enforce stopped policy: direct `jri loop resume` only when specs match the authorized fingerprint; bare `jri` start from stopped reruns audit/planning only after changed or missing specs are reconciled and reauthorized.
  - Reject invalid runner phase values inside `runLoopProcess` as well as at CLI parsing, and convert malformed/missing handoff parser failures into structured loop failure/recovery evidence.
  - Add coverage for stale lock ownership, dead runner repair, halt/stop races, forceful halt, lock mismatch, stopped start/resume boundaries, and resume after audit/planning/build.

- P0: Enforce git commit/tag and validation safety.
  - Completed/Tested: Runtime now requires passing validation evidence before accepting a git-changing successful builder handoff, records a tag only when exactly one tag points at the new commit and it is the next patch semver tag, suppresses invalid/ambiguous tag success, and fails `blocked`/`failedValidation` handoffs that changed commits or tags without emitting `commitCreated`/`tagCreated`. Focused validation passed with `bun test tests/daemon-runtime.test.ts`; final serial validation passed with `bun run test` (127 tests), `bun run typecheck`, and `bun run lint`.
  - Remaining: read validation commands from `AGENTS.md`/project guidance, record what ran, and record why stronger validation was unavailable when commands are absent or unsafe.
  - Remaining: preserve changed files for inspection on validation failure/blockers and keep destructive rollback behind explicit halt/reset policy across all failure shapes.
  - Remaining coverage: no-op success, blocked with unexpected git changes, absent validation commands, unsafe validation commands, and missing-tag evidence.

- P0: Complete human-task verifier and blocked recovery.
  - Completed/Tested: default `done` now verifies machine-checkable success criteria such as env presence and project-relative path/file existence without exposing secret values; unsupported criteria remain blocked with updated guidance; and `blocked[ambiguousSpecs]` receives spec-resolution guidance instead of human-task verification. Validation passed with focused `bun test tests/chat.test.ts`, final serial `bun run test` (129 tests), `bun run typecheck`, and `bun run lint`.
  - Reject resume when specs changed after the blocker, verification is inconclusive, the blocker is not human-task, or no active loop id/fingerprint is available.
  - Add end-to-end tests for bare `jri` `done`, verified resume, still-blocked updates, ambiguous-spec guidance, changed-spec rejection, inconclusive verification, and no-op behavior outside eligible blocked state.

- P0: Make auth recoverable and UI-neutral.
  - Treat invalid/corrupt Pi auth cache payloads as recoverable auth state with actionable status/login guidance; `jri auth status` must not hard-fail on bad auth JSON.
  - Implement or passthrough real Pi-backed login/logout/status operations where available, and normalize unsupported passthrough errors through JRI auth result types.
  - Bare interactive `jri` should launch or guide inline auth and continue into interrogation after success; non-interactive mode should print direct recovery commands and exit cleanly.
  - Keep auth behavior in core UI-neutral, with CLI responsible for display.

- P0: Dogfood the public MVP path and update docs.
  - After the P0 runtime/CLI behavior above is in place, validate `/home/nico/just-ralph-it-dogfood/gupta-to-web` only through public JRI interfaces: bare `jri`, `jri auth ...`, `jri loop attach|stop|halt|resume`, terminal automation, and JRI-visible logs/specs/status/output.
  - Dogfood success requires deployment at `gupta-to-web.mpujia.justralph.it` plus durable artifacts for interrogation, planning, iterations, blockers, validation, deployment, commits, and tags.
  - Update `README.md` after the dogfood flow works: install/run basics, auth setup, bare `jri` workflow, loop controls, recovery paths, validation commands, and dogfood workflow.
