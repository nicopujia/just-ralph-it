# Architecture

## Structure

Every project is initialized with the same base structure:

```
<project_root>/.jri/
  tasks/
    <status: "draft" | "todo" | "doing" | "done">/
      <slug>.md
  signals/
  logs/
    diffs/<iteration number>-<slug>.diff
    external/
    ralph/<iteration number>-<ISO 8601 start datetime>.log
  state.json
  state.json.bak
  .gitignore
```

- **Tasks** are markdown files whose frontmatter is the metadata and the body is the description.
- **Signals** are files that, if present, tell the loop what to do, and whose optional contents indicate the reason which will be logged.
  - `stop`: makes the loop stop at the end of the current iteration.
  It is deleted after that or when a loop starts.
- **Logs** contain absolutely everything that happens related to JRI.
  OpenCode session exports are written under `.jri/logs/external/opencode/<session-id>.json`.
  Stale-run recovery notes are appended to `.jri/logs/recovery.log`.
  Per-iteration diff artifacts are written to `.jri/logs/diffs/<iteration>-<slug>.diff`.
  Each file contains the unified diff between `jri/<iteration-1>` and `jri/<iteration>` tags,
  capturing all changes Ralph made during that iteration.
  Diff artifacts follow the same retention policy as other logs: they are gitignored and persist until manually cleaned.
- **State** is stored in `.jri/state.json`.
  JRI writes it through a same-directory temp file and keeps `.jri/state.json.bak` as the last readable recovery copy.
  If `state.json` is invalid or partially written, JRI falls back to the backup and rewrites the primary file when it can.
  It also keeps a minimal execution journal:
  `active_attempt` tracks the current Ralph task attempt, and `attempts` keeps the durable attempt history.
  Each attempt records the task slug, iteration number, branch, timestamps, Ralph log path, optional OpenCode session ID, and final outcome when known.
  The latest explicit draft-to-todo promotion confirmation is also recorded there.

The following are `.gitignore`d:

```
logs/
signals/
state.json
state.json.bak
.state.json.tmp
.state.json.bak.tmp
```

See [@src/jri/core/schemas/](../src/jri/core/schemas) to understand the exact schema files used by runtime validation.

- `task-metadata.json` validates task markdown frontmatter.
- `state.json` validates `.jri/state.json`, including ephemeral process-tracking fields used by detached runs and `halt`.

## Agents

Their source templates live in `src/jri/core/agents/`.
`jri init` writes those templates into `.opencode/agents/` for the current project and adds ignore entries for JRI-managed agent files in the client repo `.gitignore`.
`jri upgrade` refreshes those generated agent files, updates ignore rules as needed, and untracks previously committed agent files so client repos can keep them local.
The dynamic per-task Ralph user prompt is assembled in `src/jri/core/service.py`.

There are two agents:

---

### Interrogator

Makes you **a lot** of questions and creates tasks as the interrogation goes on. 

It's in charge of losslessly mapping the user's idea into tasks, making sure that there are no ambiguities left; that way, the end result will match the user's expectations.
The conversation is as long as it needs to be to cover every detail—it may even take many hours.
The agent starts high-level, goes deeper as needed, creates draft tasks as soon as new information appears, and promotes work to `.jri/tasks/todo/` once it is implementation-ready.
If a task still has open questions, it stays in `draft`; if a promoted task turns out incomplete, the fix should be captured as additive follow-up draft work instead of rewriting the promoted task.
It also commits persisted task progress as the interrogation evolves.
Promotion must go through `jri promote [slug ...] --confirm "<user confirmation>"`, which records the approval and rejects unconfirmed promotions.

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

## Clients

Clients are the different ways the user has to interact with these agents. 

For now, there's only one client, the CLI (`jri`), and the way to see the tasks is to open the files in an editor.
In the future, there will be a hosted web app.

## Structured status output

`jri status --json` prints a machine-readable JSON payload for programmatic consumption.
The schema is designed to be stable across releases so that automation and UI layers can rely on it without scraping plain text.

### Intended consumers

- **CI/CD dashboards**: pipeline scripts that poll `jri status --json` to surface blocker counts or escalate stuck tasks.
- **Web UI (Phase VIII)**: the future hosted interface will read this payload to render task state, human escalations, and retry health.
- **Monitoring/alerting**: automated watchers that trigger on `retry_escalation.tasks_with_failures[].escalated` or non-null `run.process` to detect stuck loops.

### Schema

```json
{
  "tasks": {
    "counts": { "draft": 0, "todo": 0, "doing": 0, "done": 0 },
    "total": 0,
    "needs_human": [
      {
        "slug": "string",
        "title": "string",
        "priority": 0,
        "status": "todo | doing | done | draft",
        "depends_on": ["string"]
      }
    ]
  },
  "retry_escalation": {
    "tasks_with_failures": [
      {
        "slug": "string",
        "failed_attempts": 0,
        "max_attempts": 3,
        "escalated": false
      }
    ]
  },
  "run": {
    "iteration_number": 0,
    "started_at": "unix timestamp | null",
    "finished_at": "unix timestamp | null",
    "active_attempt": "AttemptState payload | null",
    "process": {
      "loop_pid": "int | null",
      "child_pid": "int | null",
      "log_path": "string | null",
      "detached": false
    }
  }
}
```

- `tasks.counts` maps each tracked status to its task count.
- `tasks.needs_human` lists every task assigned to `Human` across all statuses, including dependency links.
- `retry_escalation.tasks_with_failures` aggregates per-task failure counts from the attempt journal and flags tasks that have exceeded the retry threshold.
- `run` mirrors the current iteration and process state from `.jri/state.json`.
