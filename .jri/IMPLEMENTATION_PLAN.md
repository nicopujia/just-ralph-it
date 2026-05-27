# Implementation Plan

- Current confirmed state from specs/source search:
  - Completed baseline: TypeScript/Bun scaffold, project root resolution, idempotent initialization, config/status validation, public core `Project` API shape, auth status/login/logout, runtime status/event primitives, daemon IPC scaffold, stop/halt/resume scaffolds, event sequence locking, live `observeLoop` follow with stdout/event cursors, runner phase orchestration, validation events, commit/tag observation, blocker parsing, resume fingerprint checks, replan signaling, and planner/builder handoff outcomes are present.
  - Completed but partial slices: bare piped `jri` routes through `Project.chat.send()`; standalone start triggers enter the audit runner path; hidden `jri --run-explorer` now builds an isolated `pi-subagent` parent command via `--extension npm:pi-subagent`, records explorer events, and emits capped artifact-backed handoffs; controlled Pi command construction isolates several CLI flags and model selection.
  - MVP is still blocked because the primary interrogation UX, SDK harness boundary, required capabilities, daemon negotiation, attach/halt controls, and dogfood docs/tests are incomplete.

- [ ] P0: Replace canned interrogation with a real Pi-backed interrogator.
  - Current `chat.send()` records turns and handles triggers, but assistant behavior is canned/minimal and does not use Pi, reconstruct durable context, update specs/scratchpad, reconcile manual spec edits, seal/unseal topics, or run observation/blocker modes.
  - Process interrogator handoffs (`messageOnly`, `specsUpdated`, `scratchpadUpdated`, `startRequested`, `humanTaskVerified`, `humanTaskStillBlocked`) instead of inferring from fixed strings.
  - Fix `done` handling for `needsHumanTask`: run safe verification and preserve/update the blocker when inconclusive instead of trusting the user's confirmation as verified.
  - Completed/tested slice: done handling now uses a verifier gate; verified handoffs mark needsHumanTask blockers resolved, while inconclusive verification preserves/updates the blocker.
  - Reconcile chat stream persistence with the runtime spec: assistant deltas/start/finish should be stream behavior while durable `interrogation.jsonl` remains completed-turn/history material.

- [ ] P0: Replace the Pi CLI `--print` shellout with the real controlled SDK harness boundary.
  - Current harness builds `pi --print ...` commands; implement the JRI-owned SDK adapter for session construction, provider auth/model registry, resource isolation, explicit tools/capabilities, prompt/context injection, stdout/event capture, and actionable capability/auth errors.
  - Ensure loops authorized by bare `jri` start through daemon-managed lifecycle registration so local chat starts cannot become invisible to daemon status/control.
  - Keep Pi-specific concepts inside the harness adapter and preserve the public core API as JRI domain concepts.

- [ ] P0: Implement required MVP capabilities behind JRI descriptors.
  - Completed/tested slice: web search/fetch is now exposed through a JRI-owned capability descriptor and hidden `jri --run-web search|fetch` bridge around the `pi-web-access` command. Search is capped to 5 timestamped results; fetch is bounded to 12k markdown and emits artifact refs for omitted content; capability/auth/tool failures produce actionable errors; and agent prompts render concrete wrapper commands instead of encouraging ad hoc fetching.
  - Validation passed for the web capability slice: `bun test tests/harness.test.ts tests/cli.test.ts tests/capabilities.test.ts`, `bun run test` (69 pass), `bun run typecheck`, and `bun run lint`.
  - Completed/tested slice: explorer is now exposed through a JRI-owned capability descriptor/instructions, and hidden `jri --run-explorer` builds an isolated `pi-subagent` parent command via `--extension npm:pi-subagent`. It writes `.jri/logs/<loopId>/capabilities/explorer/agents/explorer.md`, uses `PI_CODING_AGENT_DIR` isolation, preserves spawn/fresh read-only defaults, 6-way concurrency, a 10-minute timeout, a 4000-character handoff cap, artifact-backed handoffs, and `subagentStarted`/`subagentFinished`/`subagentFailed` events.
  - Validation passed for the explorer capability slice: `bun run test` (74 pass), `bun run typecheck`, and `bun run lint`.
  - Remaining follow-up: halt cancellation of active explorers is still not implemented; graceful stop should still prevent new explorer work after the current boundary.

- [ ] P0: Finish CLI loop controls and state-specific UX.
  - `jri loop attach` must become the live TUI surface: merged recent/live stdout plus milestone events, stable footer, `[d]etach`, `[s]top`, no footer redraws in `stdout.log`, and concise state-specific errors for blocked/stopped/halted/idle.
  - Completed/tested slice: `jri loop attach` now renders recent stdout plus milestone events, keeps a stderr footer with `[d]etach` and `[s]top`, supports detach without stopping and stop toggling via `s`, and has regression coverage proving attach footer/control bytes do not alter `.jri/logs/<loopId>/stdout.log`. This matters because attach is the primary live control surface: operators can inspect progress, detach safely, request a stop in place, and trust persisted stdout as process output rather than terminal UI noise. Validation: `bun test tests/cli.test.ts`, `bun run test` (75 pass), `bun run typecheck`, and `bun run lint`.
  - Completed/tested slice: `jri loop halt` now supports the second rollback reset decision through CLI/runtime/daemon IPC. Reset is offered only when `currentIteration.rollbackCommit` exists and `trackedTreeCleanAtStart` is true; accepted resets run `git reset --hard`, and skipped/succeeded/failed outcomes are recorded in `loopHalted` data and halted status summaries. This matters because halt must leave durable evidence about whether JRI killed only the process or also restored tracked files.
  - Completed/tested slice: loop controls now preflight state before `attach`/`stop`/`halt`/`resume`; `attach`/`stop`/`resume` return concise state-specific recovery errors with log hints when a loop id exists; `halt` is idempotent for already halted loops and no longer prompts in that state. This matters because control commands should fail fast with the right recovery path instead of making users guess what state the daemon is in. Validation: `bun test tests/cli.test.ts`.
  - Bare `jri` needs the initialization notice, Pi-backed or fallback interactive status line, blocked auto-guide display, inline auth recovery that can continue on success, and `jri auth --help` text that lists stable commands before passthrough behavior.

- [ ] P0: Harden daemon/runtime protocol behavior for long-running dogfood.
  - Completed/tested slice: clients now negotiate daemon protocol on connect; incompatible active daemons are blocked with safe guidance, and incompatible idle daemons are shut down so a compatible daemon can be started/retried; daemon IPC tests cover these cases.
  - Completed/tested slice: stdout replay offset coverage now includes multibyte UTF-8 output so attach replay/follow cursors cannot regress back to ASCII-only assumptions. This matters because event/stdout merge correctness depends on byte offsets. Validation: `bun test tests/daemon-runtime.test.ts`.
  - Tighten status repair messages, runner crash recovery across audit/planning/build, and the runtime lock/CAS story or a documented single-daemon mutation guarantee.
  - Preserve and test stdout offsets/event cursors across replay/live attach, daemon fallback, repaired states, and process death.
  - Follow-up bug from source search: controlled harness logging appends stdout and stderr concurrently into one `stdout.log`, losing channel provenance and making ordering hard to reason about; decide whether the MVP contract needs ordered merged output only or separate channel metadata/artifacts.
  - Follow-up gap from source search: hidden web/explorer capability commands bypass daemon IPC/registry today; if unified daemon observability is required, route or record those capability executions through the daemon-owned lifecycle.

- [ ] P0: Harden durable contracts and schema exports.
  - Completed/tested slice: canonical `.jri/config.json` JSON Schema is exported from core, and handoff array validators reject whitespace-only values for `specFiles`, `questions`, blocker `steps`, `successCriteria`, and related fields.
  - Completed/tested slice: handoff extraction now requires exactly one explicit valid handoff decision per phase, rejects multiple handoff records, and fails on malformed/partial handoff-prefixed output instead of carrying forward an earlier valid decision; invalid or missing handoffs fail with actionable phase-specific recovery.

- [ ] P0: Fill MVP-critical tests before dogfood.
  - Add focused tests for Pi SDK harness fakes, Pi-subagent explorer descriptors and halt cancellation, real interrogator handoffs/spec updates/context reconstruction/manual edit reconciliation, verified vs still-blocked human-task flow, chat persistence semantics, daemon handshake/version negotiation, attach TUI controls/state errors, halt reset handling, canonical schema export, and handoff trim edge cases.
  - Web search/fetch capability errors, artifact refs, result caps, fetch bounds, timestamped results, hidden CLI bridge, descriptor-rendered prompt instructions, and validation coverage are now covered.
  - Canonical schema export and handoff trim-edge tests are now covered.
  - Halt reset handling is now covered at runtime and daemon IPC boundaries, including eligible reset execution, ineligible rollback refusal, and failed reset reporting.
  - Source-search follow-up: add end-to-end coverage for repeated builder iterations when the builder handoff returns `continue`, and for runtime consumption of broader interrogator/verifier handoffs beyond parser-only tests.
  - Keep existing validation command set as the feedback loop: `bun run test`, `bun run typecheck`, and `bun run lint`.

- [ ] P0: Dogfood only through the allowed JRI interface.
  - Validate against `/home/nico/just-ralph-it-dogfood/gupta-to-web` using only bare `jri`, `jri auth ...`, loop controls, terminal automation, and JRI-visible logs/specs/status/output.
  - Success requires deployment at `gupta-to-web.mpujia.justralph.it` plus durable artifacts explaining interrogation, planning, iterations, blockers, validation, deployment, commits, and tags.

- [ ] P1: Documentation and polish after the core dogfood loop works.
  - Expand README usage and testing docs, cover auth/loop workflows, document recovery paths, and clean up transitional Pi CLI fallback code after the SDK/subagent/web capability boundary is stable.
