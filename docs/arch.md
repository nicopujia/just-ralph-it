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

Each iteration commits its changes and tags the commit. The tag marks a recoverable snapshot. Ralph works in a separate git worktree to keep the main branch clean between iterations.

Attempt history is persisted on the main branch under `.jri/attempts/<task-slug>.json` before JRI clears Ralph's runtime state.

## Generated Structure

`jri init` commits files [@src/jri/core/template](../src/jri/core/template/) under `.jri/`, alongside `.gitignore`d files which are generated at runtime, including:

- `state.json`: active runtime state for sessions, loop tracking, and recovery.
- `state.json.bak`: backup written while updating runtime state.
- `metrics.json`: runtime summary.
- `signals/`: `stop` and `result` files used by active runs.
- `logs/`: Ralph logs, exported sessions, diffs, recovery logs, and timeline data.
- `worktree/`: where Ralph makes changes.
