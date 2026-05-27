# Primary CLI Experience

## Topic

A software builder can run `jri` in any project to continue one durable
interrogation chat until they explicitly authorize Ralph.

## Job To Be Done

When I have a software idea, I want JRI to guide me through defining it clearly
before any autonomous build loop starts, so that Ralph builds what I actually
have in mind.

## Required Behavior

- Running bare `jri` is the primary user experience.
- Project root resolution is deterministic:
  - If the current directory is inside an existing `.jri` project, use the
    nearest ancestor containing `.jri`.
  - Otherwise, if the current directory is inside a git repository, use the git
    repository root.
  - Otherwise, use the current directory and initialize git there.
- If the resolved project root has not been prepared for JRI, `jri` prepares it
  automatically.
- Preparation includes initializing git when needed, creating `.jri/*`, and
  creating the root `AGENTS.md` operational template when missing.
- Automatic preparation performs only local scaffolding: `git init` when the
  directory is not already inside a git repository, default `.jri/config.json`,
  `.jri/status.json`, `.jri/specs/`, `.jri/logs/`,
  `.jri/scratchpad.md`, and root `AGENTS.md`.
- Existing `.jri` durable files and existing root `AGENTS.md` are never overwritten.
- If a subset of scaffold files is missing, preparation creates only the missing
  files.
- If required `.jri` files are present but malformed, fail JSON/schema
  validation, or use an unsupported schema version, startup stops and shows a
  clear recovery path instead of guessing or repairing values.
- Automatic preparation prints a short notice such as
  `Initialized JRI in /path/to/project`.
- Automatic preparation does not start Ralph.
- There is no public `jri init` command in the MVP.
- `jri` always opens the Pi-backed interrogator chat for the project.
- The chat is backed by controlled Pi SDK sessions assembled from JRI-managed
  context, not by an uncontrolled persistent Pi-native session.
- The user does not choose, name, or manage chat sessions.
- JRI manages chat context itself using durable project state.
- Behind the scenes, JRI may use multiple controlled Pi SDK sessions, but the
  user experience must feel like one continuous interrogation session.
- Every user and assistant chat turn is appended to
  `.jri/logs/interrogation.jsonl`.
- Reopening `jri` does not need to replay the full transcript visually. The chat
  resumes from selected durable context: current specs, scratchpad, status,
  relevant recent events, open questions, and recent unsealed turns.
- If a Pi session fails or is discarded, JRI reconstructs the next interrogator
  session from those durable files rather than asking the user to choose a
  session.
- The interrogator remains the user's entry point even when Ralph is already
  running, blocked, stopped, or idle.
- The Pi-backed TUI displays current status data directly, so users do not need
  a separate status command to know what is happening.
- The interrogator is the only user-facing path that can authorize any new Ralph
  lifecycle.
- After a loop finishes normally and the project returns to `idle`, a later
  `just ralph it` or `ralfealo` can authorize a new lifecycle after the auditor
  passes.
- There is no public `jri loop start` command in the MVP.

## Chat Rendering

- The MVP CLI should use Pi's terminal chat UI primitives when they can be used
  with JRI-controlled SDK sessions.
- Pi may render the interactive chat, streaming assistant output, and input
  affordances, but JRI owns initialization, durable context, specs, status, loop
  lifecycle, and capability selection.
- Using Pi chat UI must not require JRI to accept ambient Pi session history,
  global config, global skills, global MCPs, or unrelated user prompt/context
  discovery.
- If Pi's chat UI primitives cannot be used without compromising controlled
  sessions, the MVP falls back to a JRI-managed interrogator REPL on stdin/stdout.
- The fallback REPL still shows the same project status line and blocked
  guidance that the Pi-backed TUI would show.
- `jri loop attach` remains a separate CLI rendering surface because it needs
  live loop observation plus `[d]etach` and `[s]top` controls.

## Initialization Artifacts

- All JRI-managed artifacts live under `.jri` to avoid polluting the target
  project.
- The deliberate root exception is `AGENTS.md`, because Ralph/Pi needs
  project-specific operational guidance.
- Specs live under `.jri/specs`.
- JRI does not write a root `PROMPT.md`; prompts live in `core` and are injected
  at runtime.
- `.jri/IMPLEMENTATION_PLAN.md` is generated during the planning phase, not
  scaffolded as product truth by initialization.

`AGENTS.md` starts as a brief operational template that Ralph can fill out over
time:

```markdown
## Build & Run

Succinct rules for how to BUILD the project:

## Validation

Run these after implementing to get immediate feedback:

- Tests: `[test command]`
- Typecheck: `[typecheck command]`
- Lint: `[lint command]`

## Operational Notes

Succinct learnings about how to RUN the project:

...

### Codebase Patterns

...
```

`AGENTS.md` is not the Ralph prompt and should not contain status updates,
progress notes, implementation plans, or generic JRI process instructions.

## Developer Loop Controls

- `jri loop attach` shows Ralph output live.
- Attach uses a merged live view: raw Ralph/Pi output is the main stream, and
  normalized milestone events are inserted sparingly as compact status lines.
- While attached, a bottom status bar exposes `[d]etach` and `[s]top`.
- Halt and resume are CLI commands in the MVP, not attach footer controls.
- The bottom status bar stays stable while output streams above it.
- Detaching leaves the Ralph loop running.
- `jri loop attach` is eligible only while status is `auditing`, `planning`, or
  `building`, including when a graceful stop has been requested.
- `jri loop attach` from `blocked`, `stopped`, `halted`, or `idle` shows a
  concise error with the next useful action and the relevant log path when one
  exists.
- Stopping toggles graceful stop. If no stop is requested, it requests a
  graceful stop at the next safe boundary. During `building`, that boundary is
  after the current outer-loop iteration finishes. During `auditing` or
  `planning`, that boundary is after the current phase finishes and before a new
  phase or build iteration starts. If stop is already requested and the loop has
  not stopped yet, it clears the request.
- `jri loop stop` from `stopped`, `halted`, or `idle` returns a concise actionable
  error.
- `jri loop stop` from `blocked` returns a concise message that the loop is
  already blocked and points to the blocked resolution guide.
- `jri loop stop` has the same stop-toggle semantics as stopping from attach.
- `jri loop halt` force-kills the running Ralph loop after a `y/N`
  confirmation.
- `jri loop halt` is idempotent; if the loop is already halted, it returns a
  concise actionable message instead of repeated kill attempts.
- After a confirmed halt, JRI asks separately whether to run `git reset --hard`
  back to the recorded rollback commit for the current iteration.
- JRI offers the reset only when it recorded a clean tracked working tree at the
  rollback point. If the tree was already dirty or the rollback point is
  unknown, JRI refuses automatic reset and explains what it can inspect instead.
- The reset confirmation defaults to `N`. Canceling or skipping reset leaves
  files untouched and is recorded in loop events.
- `git reset --hard` affects tracked files only; JRI does not delete untracked
  files in the MVP.
- `jri loop halt` from `auditing`, `planning`, or `building` may kill the
  process. `jri loop halt` from `halted` is idempotent. `jri loop halt` from
  `blocked`, `stopped`, or `idle` returns a concise actionable error and does
  not mutate status.
- `jri loop resume` resumes only after a loop was already authorized through the
  interrogator.
- `jri loop resume` from `stopped` continues the same authorized lifecycle from
  durable `.jri` state by starting the next safe phase or outer-loop iteration;
  it does not resume a previous Pi session.
- `jri loop resume` from `stopped` is rejected if specs changed after the stop.
  The actionable recovery is to run bare `jri`, reconcile the changed
  requirements, and say `just ralph it` or `ralfealo` so audit and planning
  rerun before building.
- `jri loop resume` is allowed from `blocked` only for `needsHumanTask` after
  the user has said `done` in bare `jri` and JRI has marked the blocker
  resolved.
- The `done` message is processed only by bare `jri` chat. `jri loop resume`
  does not accept `done` as a substitute for the chat verification path; it only
  continues when a verified human-task resolution is already recorded.
- `jri loop resume` is not allowed from `idle`, `halted`, active states, or
  `blocked` with `ambiguousSpecs`.
- `jri loop resume` from disallowed states returns concise actionable errors with the
  blocking reason and the next allowed command.
- `jri loop resume` from no eligible loop returns a concise actionable error.
- `jri auth` exposes Pi-backed provider authentication through JRI.
- The stable MVP auth commands are `jri auth status`, `jri auth login`, and
  `jri auth logout`.
- The `jri auth` namespace may forward additional Pi auth subcommands as an
  advanced passthrough, but unknown or unsupported operations must return a
  normalized actionable error from JRI.
- `jri auth --help` lists the stable commands first and clearly labels any
  passthrough behavior as auth-only, not general Pi access.
- If bare `jri` starts in interactive mode and needs auth before it can open the
  interrogator, it launches or guides inline auth and continues into chat on
  success.
- If inline auth cannot run (non-interactive or unsupported context), `jri`
  prints a direct recovery command such as `jri auth ...` and exits without
  opening a different product mode.
- The MVP has no public `jri status` command. Status is displayed directly in
  the Pi-backed `jri` TUI or fallback REPL.

Public MVP command surface:

- `jri`
- `jri auth ...`
- `jri loop attach`
- `jri loop stop`
- `jri loop halt`
- `jri loop resume`

## Acceptance Criteria

- A new user can run `jri` without knowing the Ralph phases.
- The CLI does not require the user to run planning or start commands manually.
- Initialization is invisible unless there is an error or useful concise status
  to report.
- JRI never starts the first Ralph build loop from a direct public start command.
- JRI never starts later Ralph build loops from a direct public start command
  either; new lifecycles are always authorized through interrogation.
- A user can observe an active Ralph run without taking ownership of the loop.
- A user has an explicit force-kill escape hatch that requires confirmation and
  does not reset git state unless separately confirmed.
- When Ralph finishes or deploys something user-visible, bare `jri` surfaces the
  final status, relevant URL or artifact, validation result, and next useful
  action through the chat/status surface without requiring `jri status`.

## Non-Goals

- The MVP does not include the web UI.
- The MVP does not include VPS provisioning.
- The MVP does not expose internal planning as a required user command.
