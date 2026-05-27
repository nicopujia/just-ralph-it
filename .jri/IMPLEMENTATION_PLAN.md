# Implementation Plan

- P0: Repair accepted-trigger and active-loop chat semantics.
  - Completed/Tested: accepted-trigger and active-loop ordering slice.
    - Deterministic trigger handling now runs before interrogator harness execution.
    - `startRequested` handoffs are revalidated against the current normalized user message.
    - In active `auditing`, `planning`, and `building` states, chat returns `attach`/`stop` guidance without invoking the interrogator/start path.
    - Focused chat tests covering this path now pass; final validation also passed with `bun test tests/chat.test.ts`, full `bun run test` (103 tests), `bun run typecheck`, and `bun run lint`.
    - Note: an initial parallel validation run timed out one CLI attach test while two TypeScript checks were also running; the attach test and full suite passed serially, so no product change was needed for that test.
  - Completed/Tested: interrogation-state reconciliation now runs on empty/open checks and ordinary non-trigger chat.
    - Core `chat` empty/open checks now invoke interrogation start-gate reconciliation before user input.
    - Non-trigger chat now runs start-gate reconciliation, and CLI bare `jri` with empty stdin displays and persists pending spec reconciliation.
    - Validation passed with focused checks plus full `bun test`, `bun run typecheck`, and `bun run lint`.
  - Remaining: Make the accepted-trigger `chat.send()` stream include daemon lifecycle events beyond `loopStarted` by keeping `loop.start` streaming or chaining observation for the newly authorized loop.

- P0: Finish durable interrogator context reconstruction and capabilities.
  - Current harness invocation passes broad refs (`.jri/specs`, `.jri/scratchpad.md`, `.jri/status.json`, full `.jri/logs/interrogation.jsonl`), and the default Pi CLI adapter only uses the last inline user message; implement selected context refs from `.jri/interrogation-state.json`: open topics, pending reconciliation, recent unsealed turns, status, relevant loop summaries, specs, and scratchpad.
  - Add topic/open-turn selection so sealed topics omit old chat logs while their spec files remain requirements truth; cover reopen after manual edit, deleted spec, added spec, and context passed to fakes.
  - Add interrogator web capability support with chat-owned owner metadata and artifacts under `.jri/logs/interrogation-artifacts/`, separate from loop events/status.
  - Remaining: `checkInterrogationStartGate` does not yet detect newly added/untracked spec files.

- P0: Replace Pi CLI shellout harness with the controlled SDK adapter contract.
  - Current `invokeDefaultHarness` and `runControlledPiSession` build `pi --print` commands; loop phases still use `HarnessSessionRunner` with `{ projectDir, loopId, phase, stdoutPath }`, bypassing `HarnessInvocation` fields for owner, context refs, capabilities, and cancellation.
  - Implement the JRI-owned Pi TypeScript SDK adapter for interrogator, auditor, planner, builder, and explorer using `HarnessInvocation` and `HarnessResult`; keep Pi package details inside the adapter and make tests/fakes use the same contract.
  - Wire loop phases through `HarnessInvocation` instead of the older session runner shape, including agent, phase, model, capabilities, context, output, signal, and invalid-handoff result mapping.
  - Honor cancellation before and after session start, including timeout and halt signals, and normalize auth, model, capability, and SDK failures into JRI errors.

- P0: Fix output, capability ownership, and cancellation invariants.
  - `runControlledPiSession` currently appends stdout and stderr concurrently to the same `stdout.log`; replace this with one ordered merged writer per loop and move channel-specific evidence into structured events, handoffs, or artifacts when needed.
  - Internal `--run-web` and `--run-explorer` currently accept projectDir/loopId/task arguments without validating current owner metadata; add owner metadata validation for current loop/chat invocation and reject missing, stale, or mismatched ownership.
  - Register loop-owned web/explorer child processes with the runner so halt cancels the runner plus children, capability timeouts share the same cancellation path, and graceful stop prevents new loop-owned capability work only at safe boundaries.
  - Ensure chat-owned capability work cannot mutate loop status or write loop events.

- P0: Harden runtime state mutation, resume, and failure recovery.
  - `acquireLock` is read/mutate/write plus reread confirmation, not a real CAS; implement race-safe lock acquisition or a single-daemon mutation guarantee that satisfies the runtime spec, with contention tests.
  - `chooseResumePhase` currently resumes stopped loops to `building` when `.jri/IMPLEMENTATION_PLAN.md` exists, otherwise `planning`; persist and resume the exact next safe phase from durable state/events instead.
  - Convert invalid or missing handoff parser failures inside `runLoopProcess` into structured loop failure events and status transitions instead of letting the runner throw out of band with only later recovery.
  - Keep existing stopped/human-task fingerprint checks and add coverage for stale lock ownership, dead runner repair, and resume after audit/planning/build boundaries.

- P0: Make handoff contracts strict and enforce validation/commit safety.
  - Completed/Tested: root handoffs plus nested blocker, resolutionGuide, validation, and artifact records now reject unknown keys; legacy builder blocker/replan prefixes still work. Validation passed with `bun test tests/handoffs.test.ts`, `bun run test` (104 tests), `bun run typecheck`, and `bun run lint`.
  - Core already records `failedValidation`, but commit/tag observation only runs after successful builder handoffs; add git-state guards for `failedValidation` and `blocked` outcomes so unexpected commits/tags become structured loop failure/recovery evidence and never emit success commit/tag events.
  - Implement validation evidence expectations from `AGENTS.md`/project operational guidance, including minimum behavior when commands are absent or unsafe, and ensure commit/tag success requires clean validation evidence.

- P0: Complete human-task verification and blocker recovery.
  - The default verifier in `chat.ts` always returns `stillBlocked`; replace it with a safe verifier path that can inspect allowed evidence/capabilities and produce `verified` or `stillBlocked` without asking users to paste secrets.
  - Add end-to-end tests for `done` from bare `jri`, `humanTaskVerified`, `humanTaskStillBlocked`, blocked status updates, verified resume, inconclusive verification, changed-spec rejection, and no-op behavior outside `blocked[needsHumanTask]`.

- P1: Replace fallback CLI chat with the intended terminal experience and richer status.
  - Bare `jri` currently uses a readline fallback; integrate Pi terminal chat UI primitives only if they work with JRI-controlled SDK sessions, otherwise harden the fallback to the same status/footer requirements.
  - Surface final idle/completed status from `lastResult`, including URL/deployment, validation result, commit, tag, artifact/log hints, and next action.
  - Keep the public CLI surface to bare `jri`, `jri auth {status|login|logout}`, and `jri loop {attach|stop|halt|resume}`; keep `--run-*` as internal adapter entrypoints.
  - Test the installed/public `jri` bin path and packaging, not only `bun src/cli/index.ts`.

- P1: Finish Pi-backed auth and auth-only passthrough.
  - `auth.login` currently returns guidance based on `OPENAI_API_KEY` or Pi auth storage; implement real Pi-backed auth operations where available, normalize unsupported passthrough errors, and keep auth behavior UI-neutral in core.
  - Interactive bare `jri` should launch or guide inline auth and continue into interrogation on success; non-interactive mode should exit with a direct recovery command.

- P1: Dogfood and document after P0 behavior is in place.
  - Validate against `/home/nico/just-ralph-it-dogfood/gupta-to-web` only through public JRI interfaces: bare `jri`, `jri auth ...`, loop controls, terminal automation, and JRI-visible logs/specs/status/output.
  - Dogfood success requires deployment at `gupta-to-web.mpujia.justralph.it` plus durable artifacts for interrogation, planning, iterations, blockers, validation, deployment, commits, and tags.
  - Update `README.md` after the flow works: install/run basics, auth setup, bare `jri` workflow, loop controls, recovery paths, validation commands, and dogfood workflow.
