# Architecture

JRI is an intent discovery and convergence system for technical owner-operators.
Its autonomy is bounded by validated intent: Interrogator converts ambiguity into explicit user-confirmed assumptions, and Ralph executes only the promoted work that follows from those assumptions.

## Agents

### [Interrogator](../src/jri/core/agents/prompts/interrogator.md)

Generates/refines tasks. Asks many questions, pressure-tests intent, and converts guesses into explicit user-confirmed assumptions before promoting work. Uses tools to manage `draft` tasks until they are ready for `todo`. One per project.

### [Ralph](../src/jri/core/agents/prompts/ralph.md)

Execution engine for exactly one task. Acts as orchestrator, spawning subagents instead of doing the work itself. Ralph's autonomy is bounded by the validated intent encoded in the task. Ralph can report three task-result outcomes in its structured payload:

- `completed`: task finished and validated.
- `incompleted`: task is retryable later, but with learnings included.
- `needs_human`: blocked by a genuine human-only action, generates a Human task from Ralph's structured payload.

Runtime outcome and Ralph result payload are separate layers. The structured `ralph-result` payload written to `.jri/signals/result` is Ralph's authoritative task-result contract. JRI interprets the runtime around that payload separately: missing or invalid payloads are JRI-level `failed` runs, while valid payloads are copied onto the persisted attempt record for auditability.

Ralph may create draft follow-up tasks only for concrete bugs or refactors discovered while executing the current task. Unrelated product or roadmap ideas are captured as concise notes or learnings instead of tasks.

Bounded cleanup means Ralph should not do extra compatibility work unless requested. That does not permit breaking user-visible behavior outside the task scope.

## Flow

```
User <-> Interrogator <-> Tasks <-> Ralph
```

The user doesn't interact directly with the tasks nor with Ralph.

### Task Lifecycle

```
draft -> todo -> doing -> done
```

- There is always **at most one** `doing` task at a time.
- Promoted (todo/doing/done) tasks are append-only; changes go in new draft tasks.

### Git History

Each iteration commits its changes and tags the commit. The tag marks a recoverable snapshot. Ralph works in a separate git worktree to keep the main branch clean between iterations.

Attempt history is persisted on the main branch under `.jri/attempts/<task-slug>.json` before JRI clears Ralph's runtime state.

## Generated Structure

`jri init` creates a git repo when needed, then commits project-owned scaffold under `.jri/` and a root `Makefile` when the repo does not already define one. The `.jri/` scaffold comes from packaged template assets. The root `Makefile` is generated explicitly in code so JRI can treat repo-root files more cautiously. The Pi prompt/skill/extension/tools bundle is installation-owned and assembled into a temporary runtime package for each chat or Ralph run. Alongside the committed scaffold, these `.gitignore`d runtime files are generated as needed:

- `state.json`: active runtime state for sessions, loop tracking, and recovery.
- `state.json.bak`: backup written while updating runtime state.
- `metrics.json`: runtime summary.
- `signals/`: `stop` and `result` files used by active runs.
- `logs/`: Ralph logs, exported sessions, diffs, recovery logs, and timeline data.
- `worktree/`: where Ralph makes changes.
