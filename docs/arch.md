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
    external/
    ralph/<iteration number>-<ISO 8601 start datetime>.log
  state.json
  .gitignore
```

- **Tasks** are markdown files whose frontmatter is the metadata and the body is the description.
- **Signals** are files that, if present, tell the loop what to do, and whose optional contents indicate the reason which will be logged.
  - `stop`: makes the loop stop at the end of the current iteration.
  It is deleted after that or when a loop starts.
- **Logs** contain absolutely everything that happens related to JRI.
  OpenCode session exports are written under `.jri/logs/external/opencode/<session-id>.json`.

The following are `.gitignore`d:

```
logs/
signals/
state.json
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
If a task still has open questions, it stays in `draft`; if it was promoted too early, it should be moved back to `draft` until clarified.
It also commits persisted task progress as the interrogation evolves.

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
