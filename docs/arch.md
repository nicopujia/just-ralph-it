# Architecture

## Agents

### [Interrogator](../src/jri/core/template/.opencode/agents/interrogator.md)

Generates/refines tasks. Asks many questions and uses tools to manage `draft` tasks until they are ready for `todo`. One per project.

### [Ralph](../src/jri/core/template/.opencode/agents/ralph.md)

Solves exactly one task. Acts as orchestrator, spawning subagents instead of doing the work itself. Ralph can report three outcomes:

- `completed`: task finished and validated.
- `incomplete`: task is retryable later, but with learnings included.
- `needs_human`: blocked, generates a Human task from Ralph's structured payload.

The structured `ralph-result` payload written to `.jri/signals/result` is the sole authoritative Ralph run result contract. Missing or invalid payloads are treated as JRI-level `failed` runs.

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

Each iteration commits its changes and tags the commit. The tag marks a recoverable snapshot; `jri reset` rolls back to the latest tag, or to a specific task tag when you pass a slug. Ralph works in a separate git worktree to keep the main branch clean between iterations.

Attempt history is persisted on the main branch under `.jri/attempts/<task-slug>.json` before JRI clears Ralph's runtime state.

## Generated Structure

`jri init` commits files [@src/jri/core/template](../src/jri/core/template/) under `.jri/`. Git-ignored files are generated at runtime.
