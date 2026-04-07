# Architecture

## Structure

Every project is initialized with the same base structure:

```toml
<project-root>/
    .opencode/
        agents/
            interrogator.md
            ralph.md
        tools/
            result.js
        .gitignore
    .jri/
        tasks/
            <status: "draft" | "todo" | "doing" | "done">/
                <slug>.md
        signals/
            stop
            result
        worktree/
        logs/
            diffs/
                <iteration number>-<slug>.diff
            external/
            ralph/
                <iteration number>-<ISO 8601 start datetime>.log
            timeline.jsonl
        .gitignore
        state.json
        state.json.bak
    README.md
    opencode.json
```

- **Tasks** are markdown files whose frontmatter is the metadata and the body is the description.
- **Signals** are files that, if present, tell the loop what to do, and whose optional contents indicate the reason which will be logged.
  - `stop`: makes the loop stop at the end of the current iteration.
  It is deleted after that or when a loop starts.
- **Logs** include all JRI's observability.
  See [Operations](./ops.md) for the full location reference.
- **State** is stored in `.jri/state.json`.
  JRI writes it through a same-directory temp file and keeps `.jri/state.json.bak` as the last readable recovery copy.
  If `state.json` is invalid or partially written, JRI falls back to the backup and rewrites the primary file when it can.
  It also keeps a minimal execution journal:
  `active_attempt` tracks the current Ralph task attempt, and `attempts` keeps the durable attempt history.
  Each attempt records the task slug, iteration number, branch, timestamps, Ralph log path, optional OpenCode session ID, and final outcome when known.
  The latest explicit draft-to-todo promotion confirmation is also recorded there.
- **Worktree** is where Ralph works.

See [@src/jri/core/schemas/](../src/jri/core/schemas) to understand the exact schema files used by runtime validation.

- `task-metadata.json` validates task markdown frontmatter.
- `state.json` validates `.jri/state.json`, including ephemeral process-tracking fields used by detached runs and `halt`.

## Agents

Their source templates live [@src/jri/core/agents](../src/jri/core/agents/).
`jri init` writes those templates into `.opencode/agents/` for the current project and adds ignore entries for JRI-managed agent files in the client repo `.gitignore`.
`jri upgrade` refreshes those generated agent files, updates ignore rules as needed, and untracks previously committed agent files so client repos can keep them local.

There are two agents:

---

### Interrogator

Makes you **a lot** of questions and creates tasks as the interrogation goes on. 

It's in charge of losslessly mapping the user's idea into tasks, making sure that there are no ambiguities left; that way, the end result will match the user's expectations.
The conversation is as long as it needs to be to cover every detail—it may even take many hours.
The agent starts high-level, goes deeper as needed, creates draft tasks as soon as new information appears, and promotes work to `.jri/tasks/todo/` once it is implementation-ready.
If a task still has open questions, it stays in `draft`; if a promoted task turns out incomplete, the fix should be captured as additive follow-up draft work instead of rewriting the promoted task.
It also commits persisted task progress as the interrogation evolves.
Promotion goes through `jri promote [slug ...]`, which shows the tasks to be promoted and asks for `y/N` confirmation (use `--force` to skip the prompt).

Besides, if the user ever realizes that they actually want to pivot or discard the idea, that's not a failure scenario; it's rather the contrary.

See prompt [@src/jri/core/agents/interrogator.md](../src/jri/core/agents/interrogator.md).

Only one Interrogator is spawned per project.

### Ralph

Solves **only one** task.

It has root access and all permissions allowed on the machine, so it will do all on its power to solve the task, making sure to test the software *just as a human developer would do*.
It commits frequently and, when writing code, follows TDD principles.
It also acts as an orchestrator, spinning up to 50 subagents for reads and up to 10 for implementation, rather than doing the work by itself; that way, it ensures to keep its context window lean.

Phase II gives Ralph exactly three runtime outcomes:

- `completed` when the task is finished and validated
- `failed` when the run did not complete successfully
- `needs human` when progress requires human input or action

When Ralph resolves to `needs human`, the current iteration aborts and recovery creates a generated `Human` task in `todo` with the required context.
The original Ralph task also returns to `todo` and is blocked via `depends_on` on that generated Human task until the human work is done.
Besides, if Ralph, while solving the current task, finds new ones (e.g., a bug which should be fixed), it creates them.
JRI moves the active task through `.jri/tasks/todo/`, `.jri/tasks/doing/`, and `.jri/tasks/done/`; Ralph must not edit or relocate the current task file in `doing`.

`jri start` also performs stale-run recovery before a new loop begins.
If `.jri/tasks/doing/` contains exactly one task but the tracked loop PID is missing or dead, JRI treats the run as interrupted.
If the active attempt has not applied completion side effects yet, recovery moves the task back to `todo`, records that attempt as `interrupted`, clears in-progress runtime state (`started_at` and tracked process metadata), and commits the task move as `jri: recover <slug> after stale run`.
If the repo already contains the completed result for that attempt, recovery finishes the remaining bookkeeping from the attempt record instead of rerunning Ralph.
That is the idempotency contract for interrupted retries: retries may create a new attempt, but they must not duplicate already-applied completion side effects.

Failed work is automatically retried up to three times before escalating to `needs human`.
Each failed attempt is persisted in the attempt journal as `outcome: "failed"`.
When a task accumulates three failed attempts, the loop auto-escalates it: it creates a generated `Human` task, blocks the original task via `depends_on`, and skips it on subsequent runs until the human blocker is resolved.
A failure stays retryable as long as the total failed attempt count for that task slug is below the threshold; once it reaches three, only manual intervention (resolving the generated Human task) can unblock it.

Foreground starts continue into the loop immediately after recovery.
Detached starts recover first and then launch a fresh background loop.
If the tracked loop PID is still alive, `jri start` refuses to start a second loop.

See prompt [@src/jri/core/agents/ralph.md](../src/jri/core/agents/ralph.md).

---

Neither Ralph nor the user are intended to edit promoted tasks; draft-task editing is exclusive to the Interrogator, and draft-to-todo promotion requires explicit user confirmation.
Once a task is promoted into `todo`, `doing`, or `done`, its committed git content becomes append-only.
If the task needs correction, capture that as additive follow-up work in a new `draft` task instead of silently rewriting the promoted file in place.
Likewise, the user never interacts directly with Ralph, only with Interrogator.

```
User <-> Interrogator <-> Tasks <-> Ralph
```

## Stop and Halt Semantics

JRI provides two distinct mechanisms for terminating the loop: graceful stop and hard halt.

### Graceful Stop (`jri stop`)

The graceful stop command creates a signal file at `.jri/signals/stop`. The loop checks for this signal at the end of each iteration. If found, the loop stops gracefully after completing the current task. The signal file is deleted at the start of a new `jri start` invocation. Optional reason text can be provided when creating the stop signal and is preserved in the signal file for logging.

### Hard Halt (`jri halt`)

The hard halt command sends SIGTERM to the tracked Ralph process and clears process tracking state from `.jri/state.json`. It works on both foreground and detached runs. The command uses process group killing when possible to ensure child processes are terminated. If no process is currently tracked, a `JriError` is raised.

After a graceful stop, task state is clean. After a halt, the doing task may remain and will be recovered on the next `jri start`. Both compose cleanly with start recovery semantics.

## Status command

`jri status` prints a plain-text summary of task counts by status and lists all tasks assigned to Human.

The future web UI (Phase VIII) will access project state by importing `jri.core` directly, not by parsing CLI output.
No CLI output format is considered stable at this point.
