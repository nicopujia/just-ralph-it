# Implementation Plan

- Current confirmed state from specs/source search:
  - Completed baseline: TypeScript/Bun scaffold, project root resolution, idempotent initialization, config/status validation, public core `Project` API shape, auth status/login/logout, runtime status/event primitives, daemon IPC scaffold, stop/halt/resume scaffolds, event sequence locking, live `observeLoop` follow with stdout/event cursors, runner phase orchestration, validation events, commit/tag observation, blocker parsing, resume fingerprint checks, replan signaling, and planner/builder handoff outcomes are present.
  - Completed but partial slices: bare piped `jri` routes through `Project.chat.send()`; standalone start triggers enter the audit runner path; hidden `jri --run-explorer` records explorer events and capped artifact-backed handoffs; controlled Pi command construction isolates several CLI flags and model selection.
  - MVP is still blocked because the primary interrogation UX, SDK harness boundary, required capabilities, daemon negotiation, attach/halt controls, and dogfood docs/tests are incomplete.

- [ ] P0: Replace canned interrogation with a real Pi-backed interrogator.
  - Current `chat.send()` records turns and handles triggers, but assistant behavior is canned/minimal and does not use Pi, reconstruct durable context, update specs/scratchpad, reconcile manual spec edits, seal/unseal topics, or run observation/blocker modes.
  - Process interrogator handoffs (`messageOnly`, `specsUpdated`, `scratchpadUpdated`, `startRequested`, `humanTaskVerified`, `humanTaskStillBlocked`) instead of inferring from fixed strings.
  - Fix `done` handling for `needsHumanTask`: run safe verification and preserve/update the blocker when inconclusive instead of trusting the user's confirmation as verified.
  - Reconcile chat stream persistence with the runtime spec: assistant deltas/start/finish should be stream behavior while durable `interrogation.jsonl` remains completed-turn/history material.

- [ ] P0: Replace the Pi CLI `--print` shellout with the real controlled SDK harness boundary.
  - Current harness builds `pi --print ...` commands; implement the JRI-owned SDK adapter for session construction, provider auth/model registry, resource isolation, explicit tools/capabilities, prompt/context injection, stdout/event capture, and actionable capability/auth errors.
  - Ensure loops authorized by bare `jri` start through daemon-managed lifecycle registration so local chat starts cannot become invisible to daemon status/control.
  - Keep Pi-specific concepts inside the harness adapter and preserve the public core API as JRI domain concepts.

- [ ] P0: Implement required MVP capabilities behind JRI descriptors.
  - Add bounded web search/fetch capability using wrapped `pi-web-access`: up to 5 search results, bounded markdown fetches, citations, retrieval timestamps, artifact refs for omitted/large content, timeout/redirect/size limits, and explicit degraded-or-blocked behavior when web facts are required but unavailable.
  - Replace the current explorer CLI wrapper with wrapped `pi-subagent` behind a JRI `explorer` capability descriptor. Preserve spawn/fresh read-only default, 6-way concurrency, 10-minute timeout, 4000-character handoff cap, artifacts, and `subagentStarted`/`subagentFinished`/`subagentFailed` events.
  - Add JRI-owned capability descriptors/instructions for web and explorer, prevent inherited user Pi packages/settings, and cancel active explorers on halt while graceful stop prevents new explorer work after the current boundary.

- [ ] P0: Finish CLI loop controls and state-specific UX.
  - `jri loop attach` must become the live TUI surface: merged recent/live stdout plus milestone events, stable footer, `[d]etach`, `[s]top`, no footer redraws in `stdout.log`, and concise state-specific errors for blocked/stopped/halted/idle.
  - `jri loop halt` needs the second `git reset --hard` confirmation path, rollback eligibility checks, tracked-file reset execution, skipped/failed reset handling, and matching `loopHalted` status/event details.
  - Bare `jri` needs the initialization notice, Pi-backed or fallback interactive status line, blocked auto-guide display, inline auth recovery that can continue on success, and `jri auth --help` text that lists stable commands before passthrough behavior.

- [ ] P0: Harden daemon/runtime protocol behavior for long-running dogfood.
  - The daemon has a `handshake` method, but clients do not negotiate protocol versions; add client handshake/version checks, restart an incompatible idle daemon, and surface safe actions when an incompatible active daemon is running.
  - Tighten state-specific actionable errors for stop/halt/resume/attach, status repair messages, runner crash recovery across audit/planning/build, and the runtime lock/CAS story or a documented single-daemon mutation guarantee.
  - Preserve and test stdout offsets/event cursors across replay/live attach, daemon fallback, repaired states, and process death.

- [ ] P0: Harden durable contracts and schema exports.
  - Completed/tested slice: canonical `.jri/config.json` JSON Schema is exported from core, and handoff array validators reject whitespace-only values for `specFiles`, `questions`, blocker `steps`, `successCriteria`, and related fields.
  - Make handoff extraction resilient to trailing/partial malformed records without silently accepting bad lifecycle decisions; invalid or missing handoffs should still fail with actionable phase-specific recovery.

- [ ] P0: Fill MVP-critical tests before dogfood.
  - Add focused tests for Pi SDK harness fakes, web search/fetch capability errors/artifacts/citations, Pi-subagent explorer descriptors and halt cancellation, real interrogator handoffs/spec updates/context reconstruction/manual edit reconciliation, verified vs still-blocked human-task flow, chat persistence semantics, daemon handshake/version negotiation, attach TUI controls/state errors, halt reset handling, canonical schema export, and handoff trim edge cases.
  - Canonical schema export and handoff trim-edge tests are now covered.
  - Keep existing validation command set as the feedback loop: `bun run test`, `bun run typecheck`, and `bun run lint`.

- [ ] P0: Dogfood only through the allowed JRI interface.
  - Validate against `/home/nico/just-ralph-it-dogfood/gupta-to-web` using only bare `jri`, `jri auth ...`, loop controls, terminal automation, and JRI-visible logs/specs/status/output.
  - Success requires deployment at `gupta-to-web.mpujia.justralph.it` plus durable artifacts explaining interrogation, planning, iterations, blockers, validation, deployment, commits, and tags.

- [ ] P1: Documentation and polish after the core dogfood loop works.
  - Expand README usage and testing docs, cover auth/loop workflows, document recovery paths, and clean up transitional Pi CLI fallback code after the SDK/subagent/web capability boundary is stable.
