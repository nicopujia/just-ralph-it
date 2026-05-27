# Implementation Plan

- P0: Repair accepted-trigger and active-loop chat semantics.
  - Completed/Tested: accepted-trigger and active-loop ordering slice.
    - Deterministic trigger handling now runs before interrogator harness execution.
    - `daemon loop.start` now streams follow-up lifecycle events after `loopStarted` by observing from the loopStarted sequence; focused `daemon-ipc`/`chat` tests pass, followed by full `bun run test` (107 tests), `bun run typecheck`, and `bun run lint`.
    - `startRequested` handoffs are revalidated against the current normalized user message.
    - In active `auditing`, `planning`, and `building` states, chat returns `attach`/`stop` guidance without invoking the interrogator/start path.
    - Focused chat tests covering this path now pass; final validation also passed with `bun test tests/chat.test.ts`, full `bun run test` (103 tests), `bun run typecheck`, and `bun run lint`.
    - Note: an initial parallel validation run timed out one CLI attach test while two TypeScript checks were also running; the attach test and full suite passed serially, so no product change was needed for that test.
  - Completed/Tested: interrogation-state reconciliation now runs on empty/open checks and ordinary non-trigger chat.
    - Core `chat` empty/open checks now invoke interrogation start-gate reconciliation before user input.
    - Non-trigger chat now runs start-gate reconciliation, and CLI bare `jri` with empty stdin displays and persists pending spec reconciliation.
    - Validation passed with focused checks plus full `bun test`, `bun run typecheck`, and `bun run lint`.
- P0: Finish durable interrogator context reconstruction and capabilities.
  - Completed/Tested: ordinary chat now builds selected interrogator context instead of passing broad refs.
    - Harness invocations now receive selected refs for `.jri/status.json`, `.jri/interrogation-state.json`, individual spec files, scratchpad, and a bounded recent-turn pseudo-ref only when topics are open or pending reconciliation.
    - Sealed topics omit old interrogation turns while their spec files remain requirements truth; the default Pi prompt path now renders the selected `HarnessInvocation.context` instead of reconstructing the full interrogation log.
    - Verified with `bun test tests/chat.test.ts tests/harness.test.ts`, full `bun run test` (111 tests), `bun run typecheck`, and `bun run lint`.
    - Note: the first full validation run was executed in parallel with typecheck/lint and hit the known timing-sensitive CLI attach timeout; the focused attach test and the full suite passed serially afterward.
  - Continue improving topic/open-turn selection with relevant loop summaries and finer-grained reopened/deleted-spec context coverage.
  - Add interrogator web capability support with chat-owned owner metadata and artifacts under `.jri/logs/interrogation-artifacts/`, separate from loop events/status.
  - Completed/Tested: added spec files under `.jri/specs` are now detected as pending `specFileAdded` reconciliation in `checkInterrogationStartGate`.
    - Verified with `bun test tests/interrogation-state.test.ts`, full `bun run test` (108 tests), `bun run typecheck`, and `bun run lint`.
  - Finding: `startRalphLoop` can bypass pending reconciliation unless the flow reaches daemon IPC; direct entry paths should still execute start-gate reconciliation consistently.

- P0: Replace Pi CLI shellout harness with the controlled SDK adapter contract.
  - Current `invokeDefaultHarness` and `runControlledPiSession` build `pi --print` commands; loop phases still use `HarnessSessionRunner` with `{ projectDir, loopId, phase, stdoutPath }`, bypassing `HarnessInvocation` fields for owner, context refs, capabilities, and cancellation.
  - Implement the JRI-owned Pi TypeScript SDK adapter for interrogator, auditor, planner, builder, and explorer using `HarnessInvocation` and `HarnessResult`; keep Pi package details inside the adapter and make tests/fakes use the same contract.
  - Wire loop phases through `HarnessInvocation` instead of the older session runner shape, including agent, phase, model, capabilities, context, output, signal, and invalid-handoff result mapping.
  - Honor cancellation before and after session start, including timeout and halt signals, and normalize auth, model, capability, and SDK failures into JRI errors.
  - Finding: malformed daemon IPC payloads currently can bubble as raw `SyntaxError`; IPC parsing should map malformed JSON to `daemon-protocol-error` for predictable failure behavior.

- P0: Fix output, capability ownership, and cancellation invariants.
  - `runControlledPiSession` currently appends stdout and stderr concurrently to the same `stdout.log`; replace this with one ordered merged writer per loop and move channel-specific evidence into structured events, handoffs, or artifacts when needed.
  - Internal `--run-web` and `--run-explorer` currently accept projectDir/loopId/task arguments without validating current owner metadata; add owner metadata validation for current loop/chat invocation and reject missing, stale, or mismatched ownership.
  - Register loop-owned web/explorer child processes with the runner so halt cancels the runner plus children, capability timeouts share the same cancellation path, and graceful stop prevents new loop-owned capability work only at safe boundaries.
  - Ensure chat-owned capability work cannot mutate loop status or write loop events.

- P0: Harden runtime state mutation, resume, and failure recovery.
  - `acquireLock` is read/mutate/write plus reread confirmation, not a real CAS; implement race-safe lock acquisition or a single-daemon mutation guarantee that satisfies the runtime spec, with contention tests.
  - Completed/Tested: `chooseResumePhase` now resumes from durable loop state, not file presence.
    - `loopStopped` now records `nextPhase`.
    - `resume` now reads the latest durable `loopStopped.nextPhase` and uses that as the resume phase.
    - Missing or invalid `nextPhase` evidence now fails safely instead of heuristically inferring resume phase from `.jri/IMPLEMENTATION_PLAN.md`.
    - Validation passed with focused daemon-runtime tests, `bun run test`, `bun run typecheck`, and `bun run lint`.
  - Finding: malformed/missing handoff parser failures in `runLoopProcess` still need structured loop failure handling.
  - Keep existing stopped/human-task fingerprint checks and add coverage for stale lock ownership, dead runner repair, and resume after audit/planning/build boundaries.

- P0: Make handoff contracts strict and enforce validation/commit safety.
  - Completed/Tested: root handoffs plus nested blocker, resolutionGuide, validation, and artifact records now reject unknown keys; legacy builder blocker/replan prefixes still work. Validation passed with `bun test tests/handoffs.test.ts`, `bun run test` (104 tests), `bun run typecheck`, and `bun run lint`.
  - Core already records `failedValidation`, but commit/tag observation only runs after successful builder handoffs; add git-state guards for `failedValidation` and `blocked` outcomes so unexpected commits/tags become structured loop failure/recovery evidence and never emit success commit/tag events.
  - Implement validation evidence expectations from `AGENTS.md`/project operational guidance, including minimum behavior when commands are absent or unsafe, and ensure commit/tag success requires clean validation evidence.

- P0: Complete human-task verification and blocker recovery.
  - The default verifier in `chat.ts` always returns `stillBlocked`; replace it with a safe verifier path that can inspect allowed evidence/capabilities and produce `verified` or `stillBlocked` without asking users to paste secrets.
  - Add end-to-end tests for `done` from bare `jri`, `humanTaskVerified`, `humanTaskStillBlocked`, blocked status updates, verified resume, inconclusive verification, changed-spec rejection, and no-op behavior outside `blocked[needsHumanTask]`.
  - Finding: `ambiguousSpecs` with `done` currently returns a generic blocker message; return explicit guidance for the user about how to resolve ambiguous specification state instead of `no human-task blocker` text.

- P1: Replace fallback CLI chat with the intended terminal experience and richer status.
  - Bare `jri` currently uses a readline fallback; integrate Pi terminal chat UI primitives only if they work with JRI-controlled SDK sessions, otherwise harden the fallback to the same status/footer requirements.
  - Surface final idle/completed status from `lastResult`, including URL/deployment, validation result, commit, tag, artifact/log hints, and next action.
  - Keep the public CLI surface to bare `jri`, `jri auth {status|login|logout}`, and `jri loop {attach|stop|halt|resume}`; keep `--run-*` as internal adapter entrypoints.
  - Test the installed/public `jri` bin path and packaging, not only `bun src/cli/index.ts`.

- P1: Finish Pi-backed auth and auth-only passthrough.
  - `auth.login` currently returns guidance based on `OPENAI_API_KEY` or Pi auth storage; implement real Pi-backed auth operations where available, normalize unsupported passthrough errors, and keep auth behavior UI-neutral in core.
  - Interactive bare `jri` should launch or guide inline auth and continue into interrogation on success; non-interactive mode should exit with a direct recovery command.
  - Finding: invalid Pi auth JSON can hard-fail `auth status`; invalid/corrupt auth cache payloads should be treated as recoverable state and handled with status guidance.

- P1: Dogfood and document after P0 behavior is in place.
  - Validate against `/home/nico/just-ralph-it-dogfood/gupta-to-web` only through public JRI interfaces: bare `jri`, `jri auth ...`, loop controls, terminal automation, and JRI-visible logs/specs/status/output.
  - Dogfood success requires deployment at `gupta-to-web.mpujia.justralph.it` plus durable artifacts for interrogation, planning, iterations, blockers, validation, deployment, commits, and tags.
  - Update `README.md` after the flow works: install/run basics, auth setup, bare `jri` workflow, loop controls, recovery paths, validation commands, and dogfood workflow.
