# Ralph Loop Observability

## Topic

JRI makes the Ralph outer loop observable while leaving task choice to Ralph.

## Job To Be Done

When Ralph is running autonomously, I want to know what is happening now and
what happened before, so that I can trust, inspect, detach from, or gracefully
stop the loop.

## Loop Ownership

- JRI owns the outer loop lifecycle.
- A per-user local JRI daemon manages authorized Ralph loop lifecycles in the
  MVP.
- Ralph owns the inner task execution.
- Each outer-loop iteration starts a fresh agent session.
- During each iteration, Ralph studies durable files, chooses the most important
  task, makes changes, validates them, updates state, and commits when
  appropriate.
- One successful outer-loop iteration that changes tracked files should produce one
  coherent commit.
- Ralph creates the commit after completing the task, running relevant
  validation, updating `.jri/IMPLEMENTATION_PLAN.md`, and updating `AGENTS.md`
  if operational learnings changed.
- If validation fails, the iteration is not committed and no `commitCreated` event
  is emitted. If the iteration is a no-op (no tracked-file changes), no commit
  occurs.
- Validation commands come from the target project's operational guide when
  present, especially the root `AGENTS.md` Validation section. If no concrete
  validation command is available or safe for the current project, Ralph records
  validation evidence explaining what was checked and why stronger validation was
  unavailable.
- Ralph, not core, runs those project validation commands in the MVP. Core records
  the builder's validation handoff evidence, emits validation events, and refuses
  to treat a git-changing iteration as successful when passing validation evidence
  is absent.
- Core guards validation failure and blocker outcomes by comparing git state from
  iteration start with git state after the builder handoff. If a
  `failedValidation` or `blocked` handoff is accompanied by a new commit or tag,
  JRI treats that as a loop failure/recovery issue, records the unexpected git
  state for inspection, and does not emit successful `commitCreated` or
  `tagCreated` events for that iteration. Destructive rollback still requires the
  explicit halt/reset policy or a future spec.
- JRI observes and records commit hashes; it does not create commits itself in
  the MVP.
- Tag policy follows original Ralph: when there are no build or test errors,
  the iteration has a clean validation outcome and a successful change commit,
  Ralph creates a git tag. If there are no existing semantic version tags, start
  at `0.0.1`; otherwise increment the patch version of the highest semantic
  version tag. Non-semver tags are ignored for this calculation.
- Ralph commits locally only in the MVP. It does not push unless JRI is
  explicitly configured to allow push behavior in a later version.
- JRI does not parse `.jri/IMPLEMENTATION_PLAN.md` to choose tasks in the MVP.
- JRI does not interrupt the current inner task during graceful stop.
- Graceful stop is a toggle. If no stop is requested, `jri loop stop` or
  `[s]top` requests a graceful stop at the next safe boundary. During
  `building`, that boundary is after the current outer-loop iteration. During
  `auditing` or `planning`, that boundary is after the current phase and before
  a new phase or build iteration starts. If a stop is already requested and the
  loop has not stopped yet, the same action clears the stop request.
- Repeated stop actions emit `stopRequested` with the resulting
  `{ requested: boolean }` value.
- `jri loop halt` may force-kill the running loop after confirmation.
- Halt wins over a pending graceful stop. After confirmed halt, status becomes
  `halted`.
- Repeated halt on an already halted loop is idempotent and reports that the
  loop is already halted.
- After halting, JRI asks separately whether to run `git reset --hard` back to
  the recorded rollback commit for the current iteration.
- The rollback commit is the last commit JRI recorded before the current
  outer-loop iteration began, and JRI offers automatic reset only if the tracked
  working tree was clean at that rollback point.
- If reset is skipped, canceled, unavailable, or fails, JRI leaves files as-is
  and records the outcome in loop events.
- If Ralph detects insufficient, ambiguous, or contradictory specs, the loop
  finishes the current iteration and stops blocked with reason
  `ambiguousSpecs`.
- If Ralph needs a human task before it can continue, such as providing
  identity, credentials, billing access, account setup, or other external human
  action, the loop finishes the current iteration and stops blocked with reason
  `needsHumanTask`.
- If an iteration ends blocked, Ralph does not commit. Working tree changes
  remain visible, and JRI records the blocker plus changed files in logs/status.
- An outer-loop iteration begins at `iterationStarted` and ends at
  `iterationFinished`. It includes the builder session, relevant validation,
  plan/AGENTS updates, and the decision to commit, no-op, fail validation, or
  report a blocker.

## Durable Files

- `.jri/specs/*` are the source of truth for what should be built.
- Ralph must ignore `.jri/scratchpad.md`; it is interrogator-only working
  memory and not requirements truth.
- `.jri/IMPLEMENTATION_PLAN.md` is generated by the planner during the planning
  phase.
- Ralph is allowed and expected to modify `.jri/IMPLEMENTATION_PLAN.md`.
- `.jri/IMPLEMENTATION_PLAN.md` is updated by Ralph during building.
- `AGENTS.md` is a root operational guide for build, run, test, lint,
  typecheck, validation commands, and operational learnings.
- Ralph prompts live in `core` and are injected at runtime.
- JRI run history lives under `.jri/logs`.

## Plan Disposability

- `.jri/IMPLEMENTATION_PLAN.md` is disposable shared loop state.
- The user is not responsible for deleting, cleaning, or regenerating the plan.
- The orchestrator decides when regeneration is needed.
- The planner performs regeneration.
- Ralph builder may request regeneration when the plan is stale, cluttered,
  contradictory, or off track.
- JRI internally reruns the planning phase when specs materially change, the
  plan is stale, Ralph appears off track, completed items clutter the plan, or
  the current state is confusing.
- After first successful specs audit, the orchestrator always runs the planner
  to create `.jri/IMPLEMENTATION_PLAN.md`.
- After specs change before build, the orchestrator reruns the auditor and then
  the planner.
- After an `ambiguousSpecs` blocker is resolved, the orchestrator reruns the
  auditor and then the planner.
- During build, Ralph may record `needsReplan`; after that iteration the
  orchestrator emits `planRegenerationRequested`, enters `planning`, runs the
  planner, and resumes `building`.
- Regeneration produces a new plan derived from specs and current code.
- Building always lets Ralph choose the most important task from the current
  plan.
- Plan regeneration emits `planRegenerationRequested`,
  `planRegenerationStarted`, and `planRegenerationFinished` events.
- Planner regeneration may modify `.jri/IMPLEMENTATION_PLAN.md`, but it does
  not commit. Builder iterations create commits.

## Observability Requirements

- JRI records enough event history to answer what happened before.
- JRI exposes enough live output to answer what is happening now.
- `jri loop attach` streams Ralph/Pi output in real time.
- Attach is a CLI behavior. Core exposes a loop observation stream; the CLI
  renders that stream and adds terminal controls.
- The attach view merges raw Ralph/Pi output with compact milestone events from
  `.jri/logs/<loopId>/events.jsonl`.
- Events carry a monotonic `sequence` and may carry `stdoutOffset`. Attach uses
  those cursors to merge event lines with raw output without duplicating replayed
  content during hot attachment.
- MVP attach starts with a compact header plus recent context from the current
  phase or iteration: the latest milestone events and up to the last 100 raw
  output lines. It then switches to live streaming.
- Raw output remains the main stream; event lines are sparse and used for
  important milestones such as iteration starts, commits, tags, blockers,
  validation results, stop requests, and loop end.
- The bottom status bar remains stable and exposes `[d]etach` and `[s]top`.
- Halt and resume remain CLI commands in the MVP, not attach footer controls.
- `jri loop halt` records that the process was force-killed and whether the user
  chose to reset git state.
- `.jri/logs/<loopId>/events.jsonl` stores structured loop events, including
  normalized Pi events when available.
- `.jri/logs/<loopId>/stdout.log` stores the combined stdout/stderr text stream
  from the harness and child processes. It does not store JRI-owned attach
  footer redraws.
- MVP structured events are milestone-level: loop start/finish, audit
  start/result, planning start/finish, iteration start, subagent start/result,
  validation start/result, commit/tag creation, blocker reports/resolution,
  stop requests, graceful stop, halt, status repair, and loop finish.
- Generated run summaries are not an MVP requirement; add them later only if
  events and stdout are not enough for understandable status/history.
- The Pi-backed `jri` TUI displays concise status directly.

```text
ralphing | started: 4h ago | iteration: 3 | stop: no
idle | finished: 30min ago | iterations: 3
blocked | reason: needs human task | Provide billing access for deployment
```

- The status line lives in the Pi chat chrome as a compact footer/status line.
- The status line is always visible when status is non-idle or recently changed.
- Status updates immediately from core events and also refreshes from
  `.jri/status.json` through core every few seconds as a backup. If event-driven
  and refresh-driven status disagree, the process-checked status file wins and
  the UI shows the latest recovery note.
- `ralphing` is the user-facing label for the `building` machine state.
- `iteration` includes the current running iteration.
- `stop` is shown only while Ralph is active.
- `iterations` is shown for completed or idle loop history.
- Normal completion renders as `idle` with the latest finished loop summary.
- Graceful stop renders as `stopped` with the last iteration and resume hint.
- Halt renders as `halted` with whether reset was skipped, completed, or failed.
- `blocked` expands with a reason and concise detailed description.
- When blocked, the TUI should make the full resolution guide easy to inspect
  from the chat, including exact next steps and resume instructions.
- When bare `jri` opens a blocked project, the full resolution guide is also
  shown as an automatic chat message.
- The interrogator does not have its own status state.
- When blocked because of ambiguous specs, bare `jri` returns to interrogation.
- When blocked because of a human task, bare `jri` should present the required
  task clearly and resume the loop only after the user resolves it.
- The same blocker fields appear in status and `blockerReported` events:
  reason, description, resolution guide, changed files when known, whether
  validation ran, and the resume instruction.
- `jri loop resume` is allowed from `stopped`, and from
  `blocked[needsHumanTask]` after bare `jri` records the user's `done` message
  and verification succeeds. Resume starts fresh Pi sessions from durable state.
- `blocked[ambiguousSpecs]` never resumes through `jri loop resume`; the user
  must clarify specs in bare `jri`, then say `just ralph it` or `ralfealo` so
  the auditor and planner rerun.

## Acceptance Criteria

- A user can detach from live output without stopping Ralph.
- A user can request stop without killing the current task mid-iteration.
- A user can force-kill a stuck or unwanted loop through an explicitly confirmed
  halt command.
- After a run, a user can inspect what tasks were attempted and what happened.
- Observability does not require the user to understand Ralph internals.
