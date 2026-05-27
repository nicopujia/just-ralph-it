# Agent Handoff Contracts

## Topic

JRI agents report lifecycle decisions through machine-readable handoffs.

## Job To Be Done

When the interrogator, auditor, planner, builder, or verifier finishes a phase, I
want JRI to parse a bounded explicit result instead of inferring intent from
free-form prose, so that loop state, blockers, validation, deployment results,
and follow-up actions are durable and reliable.

## Contract Rules

- Agent-facing prompts may include prose instructions, but every phase that can
  change durable JRI lifecycle state must produce one machine-readable handoff.
- Handoffs are JRI-owned contracts. They should be stable domain concepts, not
  raw Pi package output.
- Handoffs are parsed from bounded agent output, validated, persisted into
  status/events where relevant, and preserved in `.jri/logs/<loopId>/stdout.log`.
- Invalid handoff JSON is a JRI runtime error with a recovery message that names
  the expected contract and the phase that produced bad output.
- Handoffs must not include secrets. For human tasks involving credentials,
  identity, billing, or account setup, the handoff records instructions and
  verification criteria, not secret values.
- Large artifacts are referenced by stable `.jri/logs/<loopId>/artifacts/*`
  paths instead of being embedded in handoff JSON.

## Interrogator Handoffs

The interrogator may report these actions:

- `messageOnly`: no durable requirement change.
- `specsUpdated`: one or more `.jri/specs/*` files changed, with a concise
  summary for the user.
- `scratchpadUpdated`: `.jri/scratchpad.md` changed without becoming requirement
  truth.
- `humanTaskVerified`: a `needsHumanTask` blocker was verified as resolved.
- `humanTaskStillBlocked`: verification was inconclusive or failed, with an
  updated resolution guide.
- `startRequested`: the user sent a standalone accepted trigger and the start
  gate should run the auditor.

`startRequested` is valid only when trigger matching passes the rules from
`interrogation-readiness.md`. Product text that merely contains the trigger
phrase is not a start request.

## Auditor Handoffs

The auditor returns exactly one of:

- `passed`: specs are ready for the current build scope and include a
  deterministic `specsFingerprint`.
- `failed`: specs are not ready, with concrete affected spec files or topics,
  concrete ambiguity/contradiction findings, and follow-up questions for the
  interrogator.

Auditor failures do not start planning or building. They are injected into the
interrogator response and recorded as `auditFailed`.

## Planner Handoffs

The planner returns:

- `planned`: `.jri/IMPLEMENTATION_PLAN.md` was created or regenerated from the
  current specs and code, with a concise summary of the highest-priority work.
- `blocked`: planning could not safely produce a plan, with an `ambiguousSpecs`
  or `needsHumanTask` blocker that satisfies the blocker fields in
  `runtime-state.md`.

Planner regeneration uses the same contract as first planning and emits the
plan-regeneration events defined by the loop observability spec.

## Builder Handoffs

The builder returns exactly one of:

- `continue`: one coherent iteration finished successfully and another build
  iteration should start.
- `complete`: the requested scope is complete.
- `blocked`: the iteration found an `ambiguousSpecs` or `needsHumanTask`
  blocker.
- `needsReplan`: the current plan is stale, contradictory, cluttered, or off
  track, and the planner should regenerate it after the current iteration.
- `failedValidation`: validation ran and failed, with command, exit code, and
  summary.

For successful iterations, JRI observes git to discover commit and tag details
rather than trusting the builder handoff. The handoff may include a deployment
URL, artifact references, or user-visible completion summary when those are part
of the completed scope.

## Validation Handoffs

Validation evidence records:

- command;
- exit code;
- pass/fail boolean;
- concise summary;
- optional artifact references for logs.

Each validation command produces `validationStarted` and `validationFinished`
events. Validation failures prevent commit/tag creation for that iteration.

## Human-Task Verification

For `needsHumanTask`, bare `jri` processes the user's `done` message by running
verification that is safe for JRI to perform. Verification records one of:

- `verified`: the blocker resolution is recorded under `blocker.resolution`, and
  `jri loop resume` may continue the existing lifecycle.
- `stillBlocked`: status remains `blocked`, and the resolution guide is updated
  with the missing evidence or next action.

Verification should not ask the user to paste secrets into chat unless a future
spec explicitly allows that for a narrow use case.

## Acceptance Criteria

- JRI never infers lifecycle state changes from unstructured agent prose alone.
- Invalid or missing handoffs fail with actionable recovery instead of silently
  continuing.
- Blockers, validation, plan regeneration, completion summaries, and deployment
  URLs are represented consistently in events, status, and logs.
- Handoff contracts remain JRI concepts even when the underlying harness is Pi.
