# Operations

## Log Locations

| What Happened | Where to Look |
|---------------|---------------|
| Ralph's full output (stdout/stderr) | `.jri/logs/ralph/<iteration>-<timestamp>.log` |
| Execution timeline (events in order) | `.jri/logs/timeline.jsonl` |
| Recovery actions | `.jri/logs/recovery.log` |
| Recovery failures | `.jri/logs/recovery-failures.log` |
| Code changes made by Ralph | `.jri/logs/diffs/<iteration>-<slug>.diff` |
| OpenCode session export | `.jri/logs/external/opencode/<session-id>.json` |

Use `jri timeline` to display events, or read the JSONL directly for programmatic consumption.

## Promotion-readiness review

Before every draft-to-todo promotion batch, the Interrogator runs a subagent-assisted review.
The review is mandatory and happens before the user is asked for confirmation.
The number of review subagents scales with batch complexity and quantity (1-6).

The review checks two dimensions:

**Task completeness** — each draft must have testable `acceptance_criteria`, an atomic title, no unresolved ambiguities in the body, and correct priority and assignee.

**Dependency-graph sanity** — the combined graph of the promotion batch and already-promoted tasks must have no unresolved draft dependencies, no unknown references, and no cycles.
Cycle detection is enforced programmatically by `jri promote`.

If any issue is found, the batch is not promoted until all issues are resolved.

## `jri reset` contract

`jri reset` is a recovery operation that restores the repo to the last successful iteration.
It works even when the working tree is dirty or a feature branch is checked out.

### What is restored

- The default branch is hard-reset to the `jri/<iteration_number>` tag.

### What is discarded

- All commits on the default branch after that tag.
- Uncommitted changes to tracked files.
- Any leftover `ralph/*` feature branches from failed or stale runs.
- In-progress runtime state (`process`, `active_attempt`, `started_at` in `state.json`).

### What is preserved

- `iteration_number`, `finished_at`, `session`, and `branch` in `state.json`.
- The full attempt history for diagnostics.

### Preconditions

- The project must be initialized (`jri init`).
- At least one successful iteration must exist (`iteration_number >= 1`).

### Postconditions

- The default branch is checked out and points to `jri/<iteration_number>`.
- The working tree matches the tag (tracked files are clean).
- `state.json` has no `process`, `active_attempt`, or `started_at`.
- Attempt history is preserved for diagnostics.
