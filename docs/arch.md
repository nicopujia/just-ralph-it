# Architecture

## Agents

### [Interrogator](../src/jri/core/agents/interrogator.md)

Generates/refines tasks. Asks many questions and uses tools to manage `draft` tasks until they are ready for `todo`. One per project.

### [Ralph](../src/jri/core/agents/ralph.md)

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

Each iteration commits its changes and tags the commit. The tag marks a recoverable snapshot; `jri reset` rolls back to the latest tag. Ralph works in a separate git worktree to keep the main branch clean between iterations.

Attempt history is persisted on the main branch under `.jri/attempts/<task-slug>.json` before JRI clears Ralph's runtime state.

### Task Lifecycle

```
draft -> todo -> doing -> done
```

Promoted (todo/doing/done) tasks are append-only and must have non-empty `acceptance_criteria`.
Corrections go in new draft tasks.

## Generated Structure

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
│   ├── signals/
│   │   ├── stop                        # stop gracefully after current iteration
│   │   └── result                      # structured result of the active Ralph run
│   ├── logs/
│   │   ├── ralph/                      # Ralph stdout/stderr per iteration
│   │   │   └── <slug>.log
│   │   ├── diffs/                      # code changes per task
│   │   │   └── <slug>.diff
│   │   ├── external/                   # session exports
│   │   ├── timeline.jsonl              # execution events in order
│   │   ├── recovery.log                # actions taken to recover dirty state
│   │   └── recovery-failures.log       # when recovery fails
│   ├── worktree/                       # where Ralph makes changes
│   ├── state.json
│   ├── state.json.bak
│   └── learnings.md                    # notes by Ralph for its future self
├── .opencode/
│   ├── agents/
│   │   ├── interrogator.md
│   │   └── ralph.md
│   └── tools/
│       └── <tool>.*
├── opencode.json
├── Makefile                            # check target (fails by default)
└── README.md                           # empty (created by init)
```
