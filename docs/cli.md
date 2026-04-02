# Command Line Interface

Each section below represents a command for `jri <command>`.

## `init`

Scaffold base structure for JRI; read more about the base structure [@docs/arch.md](./arch.md).

Aborts with warning if already initialized.
Automatically commits only the files created or managed by initialization,
using the command run as the message (e.g., `jri init`, `jri init -f`, etc.).
It also writes agent definitions to `.opencode/agents/interrogator.md` and
`.opencode/agents/ralph.md`.

## `chat`

Alias for starting OpenCode with the Interrogator agent.

By default, it resumes the session from `.jri/state.json`, if any.
Being an alias, it also supports that anything OpenCode supports.

## `start`

Start Ralph loop on the next task from `.jri/tasks/todo/` assigned to `Ralph`, which is decided based on the following criteria, in such order:

1. No dependencies unsolved
2. Highest priority
3. Alphabetically

Raises an error if a task is already in progress.

The task is obtained programmatically and injected directly into Ralph's user prompt.

Then, to solve it:

1. Branch `ralph/<iteration-number>/<task-slug>` is created starting from clean `main`.
2. Task is moved from `.jri/tasks/todo/` to `.jri/tasks/doing/`, and committed.
3. Ralph is spawned with the goal of solving only the specified task.
4. Once Ralph considers it's done, it merges to main and stops. (Branch is kept locally as a way of logging.)
5. Task is moved to `.jri/tasks/done/` and committed, tagged after the iteration number. (Tags are used for `reset`ting `main` if anything goes wrong.)
6. State is updated accordingly.
7. Changes are pushed to remote, if any.
8. Repeat

When `--detached` is used, JRI tracks the loop and child-process metadata in
`.jri/state.json` so `halt` can terminate the running loop later.

## `reset`

Set state back as it was just after last succesful iteration by using `git reset --hard`.

## `stop`

Gracefully stop Ralph at the end of the current iteration by creating a `stop` signal file.

## `halt`

Force Ralph to stop as if it crashed by using the tracked process metadata in
`.jri/state.json`.
