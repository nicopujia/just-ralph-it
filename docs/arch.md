# Architecture

## Agents

### [Interrogator](../src/jri/core/agents/interrogator.md)

Generates/refines tasks. Asks many questions, creates `draft` tasks, promotes to `todo` once ready. One per project.

### [Ralph](../src/jri/core/agents/ralph.md)

Solves exactly one task. Acts as orchestrator, spawning subagents instead of doing the work itself. Three outcomes:

- `completed`: task finished and validated
- `failed`: retryable (up to 3 attempts, then escalates)
- `needs human`: blocked, generates Human task

## Flow

```
User <-> Interrogator <-> Tasks <-> Ralph
```

The user doesn't interact directly with the tasks nor with Ralph.

### Git History

Each iteration commits its changes and tags the commit. The tag marks a recoverable snapshot; `jri reset` rolls back to the latest tag. Ralph works in a separate git worktree to keep the main branch clean between iterations.

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
│   ├── signals/
│   │   ├── stop                        # stop gracefully after current iteration
│   │   └── result                      # outcome of last Ralph run
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
│   └── state.json.bak
│   └── learnings.md                    # Notes by Ralph for its future self
├── .opencode/
│   ├── agents/
│   │   ├── interrogator.md
│   │   └── ralph.md
│   └── tools/
│       └── result.js
├── opencode.json
├── .gitignore
└── README.md                           # empty (created by init)
```
