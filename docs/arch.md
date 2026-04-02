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
`jri init` writes those templates into `.opencode/agents/` for the current project.
The dynamic per-task Ralph user prompt is assembled in `src/jri/core/service.py`.

There are two agents:

---

### Interrogator

Makes you **a lot** of questions and creates tasks as the interrogation goes on. 

It's in charge of losslessly mapping the user's idea into tasks, making sure that there are no ambiguities left; that way, the end result will match the user's expectations. 
For doing so, it goes from high-level questions first (problem, who's it for, etc.) and, once those are clear, down to lower-level ones.
It creates the tasks as soon as the user shares new information, and writes them down under `.jri/tasks/draft/`, no matter if they're incomplete.
It also asks the user about edge cases and possibilities they might be missing, not just what they're thinking about.
Nevertheless, it absolutely **never** specs anything the user didn't agree with.
Once it considers draft tasks are ready to be implemented and make up a coherent whole, it promotes them to `.jri/tasks/todo/`, from where Ralph will pick them up.

The conversation is as long as it needs to be to cover every detail—it may even take many hours.
When the conversation gets too long relative to the context window, it gets aggressively compacted (always keeping context window under 250k tokens or 70%, whatever happens first based on the model), as every detail discussed so far should have already been saved on tasks.

Besides, if the user ever realizes that they actually want to pivot or discard the idea, that's not a failure scenario; it's rather the contrary.

Only one Interrogator is spawned per project.

### Ralph

Solves **only one** task.

It has root access and all permissions allowed on the machine, so it will do all on its power to solve the task, making sure to test the software *just as a human developer would do*.
It commits frequently and, when writing code, follows TDD principles. 
It also acts as an orchestrator, spinning up to 100 subagents, rather than doing the work by itself; that way, it ensures to keep its context window lean.

If Ralph truly cannot solve the task (e.g., if it requires human identification), it creates a new task assigned to `Human`, adds it as a dependency, and aborts, letting the next iteration continue with an unblocked task.
Besides, if Ralph, while solving the current task, finds new ones (e.g., a bug which should be fixed), it creates them.

---

Neither Ralph nor the user are intended to edit tasks; that job is exclusive to the Interrogator.
Likewise, the user never interacts directly with Ralph, only with Interrogator.

```
User <-> Interrogator <-> Tasks <-> Ralph
```
