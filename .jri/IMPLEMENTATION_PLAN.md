# Implementation Plan

- Current confirmed state from specs/source search:
  - Completed baseline: TypeScript/Bun scaffold, project root resolution, idempotent initialization, config/status validation, public core `Project` API shape, auth status/login/logout stubs, runtime status/event primitives, daemon IPC for status/observe/stop/halt/resume, event sequence locking, live loop observation, runner phase orchestration, handoff parsing/validation, validation events, commit/tag observation, blocker parsing, resume fingerprint checks for stopped loops, replan signaling, web/explorer wrapper descriptors, attach controls, halt reset prompt, and state-specific loop control errors are present.
  - Confirmed partial implementations: bare piped `jri` routes through `Project.chat.send()`; accepted triggers start audit runner scaffolding; `chat.send()` records turns and handles `done`; hidden web/explorer commands provide bounded wrapper behavior; loop attach follows stdout/events and keeps footer bytes out of `stdout.log`.
  - Completed slice: lazy interrogation-state primitives now exist, sealed spec topics detect manual edits/deleted files, and chat start-gates block pending reconciliation before daemon loop start.
  - Confirmed blockers: the primary interactive interrogator, Pi SDK harness boundary, daemon-owned start, durable interrogator state, capability ownership/cancellation, lock/CAS hardening, Pi-backed auth flow, and dogfood documentation/tests remain incomplete.
  - Spec updates completed during planning: `.jri/specs/sdk-runtime-contracts.md` now defines the SDK harness/fake contract, durable `.jri/interrogation-state.json`, daemon `loop.start`, capability process ownership, and stdout channel policy; `.jri/specs/runtime-state.md` now lists the lazy interrogation state file.

- [ ] P0: Replace canned chat with the real Pi-backed interrogator.
  - Current `src/core/chat.ts` returns fixed status copy, accepts direct triggers, and uses an injectable verifier; it does not run an interrogator agent, parse interrogator handoffs, update `.jri/specs/*`, update `.jri/scratchpad.md`, reconstruct selective context, or support observation/blocker modes.
  - Current search confirmed `parseHandoff` already validates interrogator handoffs, but `chat.send` still never invokes any harness/interrogator boundary for ordinary messages and only uses fixed `responseForStatus` text.
  - Completed/tested slice: added shared `HarnessInvocation`, `HarnessResult`, and `HarnessAdapter` types; interrogation phase prompt/model command support; injectable chat interrogator harness processing for `messageOnly`, `specsUpdated`, `humanTaskVerified`, `humanTaskStillBlocked`, and `startRequested` handoffs; and focused tests in `chat/harness`.
  - Remaining: `Project.chat.send` still uses the legacy canned ordinary-message path until the default Pi SDK adapter is production-wired; spec/scratchpad file mutation plus topic sealing/reconciliation resolution are still incomplete.
  - Implement interrogator harness invocation using the new `HarnessInvocation` contract with `agent: "interrogator"` and `phase: "interrogation"`.
  - Add lazy `.jri/interrogation-state.json` support for topic sealing, spec fingerprints, and pending manual edit reconciliation.
  - Process `messageOnly`, `specsUpdated`, `scratchpadUpdated`, `startRequested`, `humanTaskVerified`, and `humanTaskStillBlocked` handoffs instead of inferring lifecycle changes from prose or fixed strings.
  - Reconcile chat persistence with the runtime contract: stream `chatMessageStarted`/`chatMessageDelta`/`chatMessageFinished` to callers, but persist durable completed turns/history material in `interrogation.jsonl`.

- [ ] P0: Make bare `jri` the primary interactive interrogation surface.
  - Current TTY bare `jri` initializes, checks auth, prints status, and exits; it does not open the required long-lived Pi-backed TUI or fallback REPL.
  - Implement a usable bare `jri` chat loop with compact status footer/line, blocked guide presentation, inline auth recovery that can continue into interrogation, and observation mode while Ralph is running.
  - Keep public CLI surface limited to `jri`, `jri auth {status|login|logout}`, and `jri loop {attach|stop|halt|resume}`; internal `--run-*` commands remain hidden adapter entrypoints.
  - Add coverage for TTY and piped modes, blocked startup messages, auth continuation, and status rendering for active/stopped/halted/idle/completed states.

- [ ] P0: Replace Pi CLI `--print` shellouts with the controlled SDK harness adapter.
  - Current `src/core/harness.ts` builds `pi --print` commands and relies on CLI flags for isolation.
  - Implement a JRI-owned adapter around the Pi TypeScript SDK using the contract in `.jri/specs/sdk-runtime-contracts.md`: explicit owner, agent, phase, model, context refs, capabilities, output sink, and cancellation signal.
  - Ensure fake harnesses use the same request/result contract and can script chunks, handoffs, capability results, artifacts, delays, failures, auth errors, and cancellation without fake-only code paths.
  - Keep Pi SDK/package details inside the adapter and preserve public core API/domain objects as JRI concepts.

- [ ] P0: Route accepted chat triggers through daemon-owned `loop.start`.
  - Completed/tested slice: daemon IPC now exposes streaming `loop.start`; `Project.chat.send` injects daemon start; chat can stream the returned `loopStarted` event.
  - Completed/tested slice: daemon `loop.start` now carries and validates the accepted trigger; `Project.chat.send` no longer falls back to local start when the daemon is unavailable.
  - Completed/tested slice: chat start-gates now block while pending reconciliation is unresolved before daemon loop start.
  - Completed/tested slice: direct daemon `loop.start` now runs the interrogation-state start gate, so pending manual spec reconciliation cannot bypass `Project.chat.send`.
  - Completed/tested slice: daemon `loop.start` now has focused IPC coverage for active-loop rejection, unresolved `needsHumanTask` blockers, verified `needsHumanTask` resume guidance, pending reconciliation, and invalid trigger text.
  - Completed/tested slice: runner start/resume ordering was hardened so start/resume acquire a daemon-held startup lock and enter the active state before spawning, then transfer process/lock ownership to the runner PID; spawn failures restore the prior durable status.
  - Focused tests passed for chat, daemon IPC, and daemon runtime coverage.
  - Final validation for this increment: `bun run test` passed with 91 tests, `bun run typecheck` passed, and `bun run lint` passed.
  - Remaining: direct test-only `sendChat()` still has a local injected/default start path; production `Project.chat.send()` now requires daemon start.
  - Remaining: keep interrogation-state gating narrow and actionable.

- [ ] P0: Harden runtime ownership, locking, and resume safety.
  - Replace read-mutator-write lock acquisition with a real compare-and-swap or document/enforce a single-daemon mutation guarantee that satisfies `runtime-state.md`.
  - Completed/tested slice: verified `needsHumanTask` resume now enforces the authorized specs fingerprint and rejects changed specs.
  - Tighten crash recovery across audit/planning/build, process death, daemon fallback, repaired states, stdout replay offsets, and event cursors.
  - Preserve the existing legal state machine and add concurrency/race tests for lock contention and stale ownership.

- [ ] P0: Finish capability ownership and cancellation.
  - Web/explorer wrappers exist, but hidden capability commands currently operate as process wrappers rather than registered owner-aware children.
  - Register loop-owned explorer/web capability children with the runner so halt cancels them, timeouts use the same cancellation path, and graceful stop prevents new capability work only at safe boundaries.
  - Validate internal `--run-web`/`--run-explorer` owner metadata and refuse missing, stale, or mismatched project/loop ownership.
  - Write loop output through one ordered merged writer per loop; keep stdout/stderr channel-specific evidence in structured events, handoffs, or artifacts when needed.
  - Include web instructions for all agents allowed by `harness-capabilities.md`, including auditor/explorer when task-relevant.

- [ ] P0: Complete safe human-task verification and auth UX.
  - Current default human-task verifier always returns `stillBlocked`; product code needs a real safe verification agent/capability path that can produce `verified` or `stillBlocked`.
  - Ensure `done` never substitutes for resolving ambiguous specs and never asks users to paste secrets unless a future narrow spec allows it.
  - Replace guidance-only auth login with Pi-backed auth operations where available; bare `jri` should launch or guide the same flow inline and continue into interrogation after success.
  - Validate that authenticated state can create a controlled SDK session with the configured provider/model preset.

- [ ] P0: Fill MVP-critical tests before dogfood.
  - Add focused tests for SDK harness fakes, interrogator handoffs/spec updates/scratchpad updates/context reconstruction/manual edit reconciliation/topic sealing, daemon-managed start, chat stream-vs-persistence semantics, verified vs still-blocked human-task flow, auth continuation, capability ownership/cancellation, lock races, changed-spec human-task resume rejection, crash repair, and repeated builder `continue` iterations.
  - Keep existing validation command set as the feedback loop: `bun run test`, `bun run typecheck`, and `bun run lint`.

- [ ] P0: Dogfood only through the allowed JRI interface.
  - Validate against `/home/nico/just-ralph-it-dogfood/gupta-to-web` using only bare `jri`, `jri auth ...`, loop controls, terminal automation, and JRI-visible logs/specs/status/output.
  - Success requires deployment at `gupta-to-web.mpujia.justralph.it` plus durable artifacts explaining interrogation, planning, iterations, blockers, validation, deployment, commits, and tags.

- [ ] P1: Polish CLI status/control edge cases.
  - Completed/tested slice: piped `jri loop attach` now treats stdin EOF as detach so attach does not hang after scripted detach/stop controls.
  - Completed/tested slice: full-suite validation exposed the CLI attach test using the shared default daemon env; it now isolates `JRI_DAEMON_*` under the temp project to avoid parallel daemon collisions.
  - Expand fallback bare status output for active, stopped, halted, completed, failed, URL/deployment, validation, and next-action hints.
  - Explain why halt rollback reset is unavailable when there is no rollback commit or the tracked tree was dirty at iteration start.
  - Make blocked `jri loop stop` messaging match the explicit "already blocked" recovery path.
  - Test the installed/public `jri` bin path and executable packaging instead of only invoking `bun src/cli/index.ts`.

- [ ] P1: Documentation after core dogfood loop works.
  - Expand `README.md` with install/run basics, auth setup, primary bare `jri` workflow, loop controls, recovery paths, validation commands, and the dogfood workflow.
  - Document transitional Pi CLI fallback behavior only if it remains available after the SDK adapter lands.
