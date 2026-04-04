# Learnings

- `make check` is the canonical repo-wide validation command.
  It runs `ruff check .`, `ruff format --check .`, `ty check`, `python -m jri.checks.schema`, and `pytest` through `uv run`.
- `uv run python -m jri.checks.schema` validates the packaged JSON Schemas, every task file under `.jri/tasks/{draft,todo,doing,done}`, and `.jri/state.json` when it exists.
- `.jri/state.json` is now persisted crash-safely via same-directory temp-file writes plus `os.replace`, and `.jri/state.json.bak` mirrors the last readable copy.
  If the primary state file is truncated or otherwise invalid, `StateStore.load()` falls back to the backup and rewrites `state.json` when it can.
- Ralph runs now normalize missing outcome markers to an explicit `failed` result with a loud stderr warning; the only canonical runtime outcomes are `completed`, `failed`, and `needs human`.
- A `needs human` outcome is represented durably by a generated `Human` todo task plus a new `depends_on` entry on the original Ralph task.
  Recovery returns the Ralph task to `todo`, writes run context into the Human task body, and lets status/scheduling treat that backlog item as the single source of truth.
- Promoted task files in `.jri/tasks/{todo,doing,done}` are append-only once committed.
  Schema/status compare them against git-tracked content, and the run loop snapshots the active `doing` task so even committed in-place rewrites during a Ralph run are rejected; corrections belong in new draft follow-up tasks.
- Promoted task files in `.jri/tasks/{todo,doing,done}` must also include a non-empty `acceptance_criteria` list.
  Draft tasks may omit it, `validate_repo()` and runtime task loading both enforce the rule, and `tests.helpers.write_task()` auto-fills promoted test fixtures unless a test sets criteria explicitly.
- `jri start` now preflights stale runs before launching.
  A lone `doing` task with a missing or dead tracked loop PID is moved back to `todo`, in-progress runtime state is cleared, and the recovery is recorded in `.jri/logs/recovery.log`; a live tracked loop PID still blocks a second start.
- `.jri/state.json` now keeps a minimal execution-attempt journal via `active_attempt` and `attempts`.
  Recovery uses that metadata plus git state to tell first runs from retries and to finish already-applied completions without rerunning Ralph.
- Failed tasks are retried automatically up to 3 times (`_MAX_FAILED_ATTEMPTS`) across `jri start` invocations, then auto-escalated to `needs human`.
  The `_run_loop` uses both an in-memory `failed_slugs` set (prevents retrying within one loop invocation) and the persistent attempt count in `state.attempts` (crosses invocations).
  After the third failure, `_escalate_failed_task` creates a Human follow-up task and blocks the original via `depends_on`, identical to the direct Ralph `needs human` outcome path.
