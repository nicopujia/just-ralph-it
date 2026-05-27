# Interrogation Readiness

## Topic

The interrogator converts a user's idea into unambiguous specs before Ralph can
build.

## Job To Be Done

When I describe a software idea, I want JRI to question me until the intended
behavior is unambiguous, so that an autonomous builder can follow the specs
literally without producing a surprising result.

## Readiness Standard

Specs are ready only when this question has a negative answer:

> If Ralph followed the specs literally, could there be more than one
> behavioral or user-facing result that satisfied them?

If yes, the specs are ambiguous and Ralph must not start.

Readiness is only satisfied when this is true for every spec in the current
build scope, whether the topic is sealed or unsealed. Sealing only removes old
chat turns from routine context reconstruction; it does not exempt the spec from
audit.

## Spec Authoring Rules

- Specs live under `.jri/specs/*`.
- The interrogator may use `.jri/scratchpad.md` as flexible working memory for
  open questions, discussion branches, unresolved scope, and notes that are not
  yet durable spec truth.
- `.jri/scratchpad.md` has no enforced template.
- `.jri/scratchpad.md` is interrogator-only working memory. Ralph must ignore it
  entirely.
- Before an auditor pass, unresolved scratchpad scope/questions must either be
  resolved into specs or explicitly deferred out of the current build scope.
- The interrogator updates specs continuously as decisions become stable during
  chat.
- Specs are not generated only as a final batch.
- After meaningful spec updates, the interrogator summarizes what changed so the
  user can correct drift.
- Specs are JRI-managed markdown, but users may inspect and edit them manually.
- When bare `jri` resumes the interrogation chat, the interrogator detects
  manual spec edits and reconciles them into chat state; it must never silently
  overwrite user edits and should ask targeted questions on conflicts or
  ambiguity.
- The interrogator owns writing and updating specs during normal operation.
- Ralph builder reads specs as requirements truth and should not rewrite them.
- If Ralph finds specs insufficient, ambiguous, or contradictory during
  building, it records the blocker and hands control back to interrogation.

## Build-Time Spec Blockers

When Ralph discovers a spec ambiguity, insufficiency, or contradiction during
building:

- The current outer-loop iteration reaches a safe end by recording the blocker,
  preserving changed files for inspection, skipping commit/tag creation, and
  emitting `iterationFinished` with a blocked outcome.
- The Ralph loop stops blocked with reason `ambiguousSpecs`.
- JRI records the blocker in logs and status with a resolution guide.
- Bare `jri` opens the interrogation chat.
- The interrogator starts from Ralph's blocker report and asks targeted
  questions.
- After the user resolves the issue, the user must again say `just ralph it` or
  `ralfealo`.
- The specs auditor reruns.
- The implementation plan is regenerated from the clarified specs and current
  code state.
- The Ralph loop resumes from durable `.jri` state with fresh agent sessions.
- Prior uncommitted work is preserved for Ralph to inspect, continue, rework, or
  revert; JRI does not automatically reset it during spec-blocker recovery.
- The `ambiguousSpecs` blocker is not cleared until the user reissues
  `just ralph it` or `ralfealo`, and the auditor passes the readiness check.

Human-required blockers are distinct from spec blockers. If Ralph cannot
continue because it needs a human task, such as credentials, identity, billing
access, account setup, or another external action, it records a blocked state
with reason `needsHumanTask`, a detailed description, and a resolution guide.
The interrogator should present the required task clearly rather than treating
it as a spec ambiguity. `needsHumanTask` applies only when the requirements are
already behaviorally clear and the missing work is external to the product
decision. Missing product, deployment, audience, business, or behavior decisions
remain `ambiguousSpecs`.

## Blocked Resolution Guidance

When the loop is blocked, the user should not have to infer what to do from raw
logs. The Pi-backed TUI and interrogator should present a concise blocked
resolution guide.

The full guide appears as an automatic chat message when bare `jri` opens a
blocked project. The status footer keeps a concise blocked summary visible and
provides access to the guide details.

The guide includes:

- What blocked Ralph.
- Why JRI cannot safely continue.
- Exact user action steps.
- Success criteria or how to verify the action is complete.
- Whether the task may involve sensitive material such as credentials.
- What phrase or action resumes JRI after the task is done.

For `ambiguousSpecs`, the guide should include the affected topic or spec files
when known, plus the concrete questions that must be answered. After the user
answers them, the user must again say `just ralph it` or `ralfealo` so the
auditor can rerun.

For `needsHumanTask`, the guide should avoid asking the user to paste secrets
into chat unless that is explicitly safe and intended. Prefer instructing the
user to configure credentials, accounts, billing, environment variables, or
identity outside the chat, then return and confirm completion. After the user
confirms completion by saying `done` in bare `jri`, JRI verifies what it can and
records the blocker resolution. The user then runs `jri loop resume` to continue
the already-authorized lifecycle from durable state. `done` is only a
human-blocker verification signal; it is not a substitute for `just ralph it` or
`ralfealo` when specs were ambiguous. If verification is inconclusive, JRI keeps
the loop blocked and updates the resolution guide with the failed or missing
check.

## Spec Structure Rules

- Requirements are broken down from Jobs To Be Done into topics of concern.
- Each topic of concern gets one markdown spec file.
- Each topic should pass the "One Sentence Without 'And'" scope test.
- Specs define behavioral outcomes, observable results, acceptance criteria,
  edge cases, constraints, and non-goals.
- Specs avoid implementation details unless the user explicitly cares about the
  choice.
- If an internal implementation choice does not affect behavior, operational
  behavior, constraints, or maintainability requirements the user cares about,
  it may be marked as implementation freedom.
- For implementation freedoms, Ralph should prefer the simplest evolvable
  option using typical KISS and YAGNI judgment.

## Scope Discipline

When the user introduces a new Job To Be Done or topic of concern, the
interrogator should think through the branches of functionality that may need to
be covered so important behavior is not forgotten.

After identifying those branches, the interrogator should focus on the actual
scope the user wants. The way to reduce the number of questions and shorten
interrogation time is to reduce scope, not to leave ambiguities.

If a branch is out of scope, the interrogator should record that clearly as a
non-goal or deferred concern. If a branch is in scope, it must be clarified
until behavior is deterministic.

## Interrogator Posture

The interrogator is not a passive questionnaire. It should help the user reach
their true intended outcome, even when that means deciding not to build the
original idea or sharply reducing scope.

The interrogator should:

- Pressure test what the user actually wants.
- Ask about business, audience, operational, and product tradeoffs when they
  affect the intended outcome.
- Propose sensitive defaults and explain their consequences.
- Batch related questions so the user can review decisions efficiently.
- Avoid asking small serial questions when a batch would be clearer.
- Push scope reduction as the preferred way to reduce interrogation time.
- Refuse to start Ralph while behavioral ambiguity remains.
- Treat "the user realizes they do not want to build this" as a valid successful
  outcome when it better matches the user's true intent.

The design discussion that produced these specs is a prototype for the
interrogator's behavior: branch out enough to avoid missed concerns, converge
through batched decisions with recommended defaults, record durable decisions,
and keep scratchpad notes for unresolved topics.

## Context Management

Conversation is temporary. Specs are durable.

The user experience is one continuous interrogation chat per project. Internally,
JRI may create many controlled Pi SDK sessions over time, but those
implementation sessions must not be exposed as user-managed chat sessions.

`.jri/logs/interrogation.jsonl` stores every chat turn as audit/history
material.
The transcript is not injected wholesale into every future Pi session by
default.

Each interrogator invocation reconstructs context selectively from durable JRI
state:

- Current specs.
- `.jri/scratchpad.md`.
- Current status and relevant recent loop events.
- Open questions and unsealed topics.
- Recent unsealed chat turns.
- Selected older transcript excerpts only when needed.

Sealed topics' old chat turns are omitted from routine context reconstruction
unless explicitly unsealed. The current spec files for sealed topics remain part
of readiness audit and build context.

The interrogator should have an internal `sealTopic`-style action for topics
that are fully specced. When sealing a topic, the interrogator should:

- Ensure the relevant `.jri/specs/<topic>.md` file is up to date.
- Move or resolve any related notes in `.jri/scratchpad.md`.
- Mark the topic as specced in JRI-managed state.
- Ensure no unresolved questions for that topic remain in scratchpad.
- Exclude that topic's old chat turns from future context reconstruction.
- Continue interrogating remaining topics with less context pressure.

Sealing a topic must not delete `.jri/logs/interrogation.jsonl`; transcripts
remain history/audit material. Context clearing means excluding old turns from
future model context, not deleting recorded history.

If a user manually edits a sealed topic or adds new input that changes accepted
behavior, that topic is unsealed and re-enters interrogation.

Generated chat summaries are not an MVP requirement. They may be added later
only if selective reconstruction from specs, scratchpad, status, events, and
transcript excerpts proves insufficient.

## Chat While Ralph Runs

Bare `jri` always opens the Pi-backed interrogator chat. The interrogator has
no separate status state; it adapts what it can do from current project status.

When Ralph is healthy and building, that same interrogator chat operates in
observation mode.

In observation mode, the interrogator may:

- Explain what Ralph is doing from `status.json` and logs.
- Answer questions about current specs or plan.
- Record user thoughts in `.jri/scratchpad.md`.
- Offer to request a graceful stop.

In observation mode, the interrogator must not:

- Mutate `.jri/specs/*`.
- Trigger replanning.
- Change the build loop's active requirements.

If the user wants to change requirements while Ralph is building, the
interrogator should explain that Ralph is currently building from the current
specs, offer to record the thought in `.jri/scratchpad.md`, and ask whether to
request a graceful stop.

If the user requests a graceful stop to change requirements, the loop stops at
the next safe boundary and status becomes `stopped`. Bare `jri` then resumes
normal interrogation. A later `jri loop resume` may continue the same loop only
when specs did not change; after spec changes, the user must again say
`just ralph it` or `ralfealo` so the auditor and planner rerun before building.

If the loop is blocked, bare `jri` resumes normal interrogation to resolve the
blocker instead of observation mode.

## Start Gate

- The user must explicitly say `just ralph it` to request the Ralph loop.
- The Spanish equivalent trigger is `ralfealo`.
- Trigger matching is case-insensitive after trimming whitespace and final
  punctuation. It must be a standalone message; surrounding product text or
  additional instructions do not silently trigger the loop.
- The trigger phrase invokes an internal start-loop tool; it does not bypass
  readiness checks.
- The internal start-loop tool runs the specs auditor before planning or
  building.
- If the auditor fails, its feedback is injected into the interrogator so the
  interrogator can continue asking targeted questions.
- If the auditor passes, JRI runs the internal planning phase that creates or
  regenerates `.jri/IMPLEMENTATION_PLAN.md`, then starts the Ralph build loop.
- After a successful `just ralph it` or `ralfealo` transition, JRI compacts or
  clears the interrogator context. Future chat resumes from durable specs,
  status, logs, and minimal extracted state instead of carrying the full
  pre-build conversation forward.

## Specs Auditor

The specs auditor is an agent. JRI should not add a separate deterministic
pre-audit checklist unless a later concrete need appears.

The specs auditor checks that:

- Each spec maps to one JTBD topic/topic of concern.
- Each topic is scoped tightly enough for one spec file.
- Behavioral and user-facing outcomes are deterministic.
- Acceptance criteria are observable.
- Failures include concrete spec/topic gaps and concrete follow-up questions.
- Implementation details are only specified when the user cares.
- Cross-topic dependencies and constraints are clear.
- Ralph can follow the specs literally without choosing between multiple
  valid behavioral outcomes.

Auditor pass requires readiness standard satisfaction for every spec in current
build scope, including sealed topics, with no unresolved scratchpad question
left in scope.

## Acceptance Criteria

- The interrogator refuses to start Ralph when behavioral ambiguity remains.
- Auditor failures result in concrete follow-up questions, not generic
  uncertainty.
- The user is never expected to know or manage Ralph's planning/build phases.
- The user can intentionally permit implementation freedom without weakening
  behavioral clarity.
