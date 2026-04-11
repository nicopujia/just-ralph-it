# Runtime Files

This file documents only `.jri/` paths that JRI creates at runtime.

## Runtime-generated paths

- `state.json`: active runtime state for sessions, loop tracking, and recovery.
- `state.json.bak`: backup written while updating runtime state.
- `signals/`: stop and result files used by active runs.
- `logs/`: Ralph logs, exported sessions, diffs, recovery logs, and timeline data.
- `metrics.json`: runtime metrics summary.
- `worktree/`: temporary git worktree where Ralph makes changes.

## Uninstall

Uninstall JRI from a repository by deleting `.jri/`.
