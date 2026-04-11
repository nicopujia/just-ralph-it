# Architecture

## Agents

### [Interrogator](../src/jri/core/template/.opencode/agents/interrogator.md)

Generates/refines tasks. Asks many questions and uses tools to manage `draft` tasks until they are ready for `todo`. One per project.

### [Ralph](../src/jri/core/template/.opencode/agents/ralph.md)

Solves exactly one task. Acts as orchestrator, spawning subagents instead of doing the work itself. Ralph can report three outcomes:

- `completed`: task finished and validated
- `incomplete`: task is retryable later
- `needs_human`: blocked, generates a Human task from Ralph's structured payload

The structured `ralph-result` payload written to `.jri/signals/result` is the sole authoritative Ralph run result contract. Missing or invalid payloads are treated as JRI-level `failed` runs.

## Flow

```
User <-> Interrogator <-> Tasks <-> Ralph
```

The user doesn't interact directly with the tasks nor with Ralph.

### Git History

Each iteration commits its changes and tags the commit. The tag marks a recoverable snapshot; `jri ctl reset` rolls back to the latest tag, or to a specific task tag when you pass a slug. Ralph works in a separate git worktree to keep the main branch clean between iterations.

Attempt history is persisted on the main branch under `.jri/attempts/<task-slug>.json` before JRI clears Ralph's runtime state.

### Task Lifecycle

```
draft -> todo -> doing -> done
```

Promoted (todo/doing/done) tasks are append-only and must have non-empty `acceptance_criteria`.
Corrections go in new draft tasks.

## Generated Structure

`jri ctl init` commits [@template](../src/jri/core/template/) files under `.jri/`.
Runtime-generated `.jri/` paths are documented in [`../.jri/README.md`](../src/jri/core/template/README.md).

```toml
<project-root>/
├── .jri/
│   ├── tasks/
│   │   ├── draft/                      # not yet ready
│   │   │   ├── .gitkeep
│   │   │   └── <slug>.md
│   │   ├── todo/                       # queued for Ralph
│   │   │   ├── .gitkeep
│   │   │   └── <slug>.md
│   │   ├── doing/                      # Ralph working on it
│   │   │   ├── .gitkeep
│   │   │   └── <slug>.md
│   │   └── done/
│   │       ├── .gitkeep
│   │       └── <slug>.md
│   ├── attempts/
│   │   ├── .gitkeep
│   │   └── <task-slug>.json            # committed attempt history per task
│   ├── README.md
│   ├── state.json                      # initialized runtime state (gitignored)
│   ├── learnings.md                    # notes by Ralph for its future self
│   ├── .opencode/
│   │   ├── agents/
│   │   │   ├── interrogator.md
│   │   │   ├── interrogator-validator.md
│   │   │   ├── ralph.md
│   │   │   └── ralph-validator.md
│   │   └── tools/
│   │       └── <tool>.*
│   └── opencode.json
├── Makefile                            # check target (fails by default)
└── README.md                           # empty (created by init)
```
