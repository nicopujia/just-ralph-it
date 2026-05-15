# Architecture

JRI is an intent discovery and convergence system for project owners.
Its autonomy is bounded by validated intent.
Interrogator converts ambiguity into explicit user-confirmed assumptions, the Intent Compiler turns those assumptions into tasks, and Ralph executes one compiled task at a time.

## Agents

### [Interrogator](../src/jri/core/agents/bundle/interrogator/prompt.md)

Builds the Intent Graph.
Asks many questions, pressure-tests intent, and converts guesses into explicit user-confirmed assumptions before compilation.
Uses graph tools to manage topic-tree notes until the user confirms that the graph is ready for `compile-graph`.
One per project.

### [Ralph](../src/jri/core/agents/bundle/ralph/prompt.md)

Execution engine for exactly one task.
Acts as orchestrator, spawning subagents instead of doing the work itself.
Ralph's autonomy is bounded by the validated intent encoded in the task.
Ralph can report three task-result outcomes in its structured payload:

- `completed`: task finished and validated.
- `incompleted`: task is retryable later, with learnings included.
- `needs_human`: task is blocked by a genuine human-only action.
  JRI creates a separate Human task from Ralph's structured payload and leaves the original Ralph task retryable.

Runtime outcome and Ralph result payload are separate layers.
The structured `ralph-result` payload written to `.jri/signals/result` is Ralph's authoritative task-result contract.
JRI interprets the runtime around that payload separately: missing or invalid payloads are JRI-level `failed` runs, while valid payloads are copied onto the persisted attempt record for auditability.
This keeps a failed JRI runtime from being confused with Ralph's own `completed`, `incompleted`, or `needs_human` result.

Ralph may create concise todo follow-up tasks only for concrete bugs or refactors discovered while executing the current task.
Unrelated product or roadmap ideas are captured as concise notes or learnings instead of tasks.

Bounded cleanup means Ralph should not do extra compatibility work unless requested.
That does not permit breaking user-visible behavior outside the task scope.

## Flow

```
User <-> Interrogator <-> Intent Graph --compile_graph--> Tasks -> Ralph
```

The user doesn't interact directly with tasks or Ralph.
Compilation is a separate step from Ralph execution.
Successful compilation creates todo tasks, commits graph changes and tasks together, and does not start Ralph or create a tag.
Failed compilation creates no tasks and no commit.

## Intent Graph

The Intent Graph is Interrogator's project memory.
It is a topic tree of concise notes for product intent, feature decisions, open questions, constraints, and agreed behavior.
Graph paths are semantic, such as `product/onboarding/signup-flow`, not filesystem paths.

The Graph Checker validates graph structure before repository checks and compilation.
It accepts an empty graph root, counts active and archived nodes, rejects malformed topic trees, and stops archived subtrees from leaking active child detail.

The Intent Compiler reads the graph with read-only repository tools, then emits executable todo tasks.
The Interrogator tool is named `compile-graph`; the architecture seam is `compile_graph`.

### Task Lifecycle

```
todo -> doing -> done
```

Intent stays in the Intent Graph until compilation creates todo tasks.

- There is always **at most one** `doing` task at a time.
- Tasks are append-only once compiled or created; follow-up work goes in new todo tasks.
- Human blockers are todo tasks assigned to `Human`.
- Completing one with `jri complete-human <slug>` moves only that Human task to `done`, unblocks dependent Ralph work, and does not mark the original Ralph task complete.
- Every started Ralph task has persisted attempt history.
- `jri inspect` reads the active or latest attempt and can recover a safe placeholder log when the original inspect log is missing, so failed and recovered attempts stay inspectable.
- Missing local diagnostics tools, such as an unavailable LSP, are recorded as evidence rather than hidden.
- Ralph should use project-native substitutes such as `make check`, lint, typecheck, build, tests, schema checks, or a small driver when optional diagnostics are unavailable.

### Git History

Compile success commits graph changes and emitted todo task files together on the main worktree.
Compile failure creates no commit and leaves no emitted task files behind.

Ralph execution is separate from compilation.
Each Ralph iteration commits its own work on a separate git worktree to keep the invoking worktree's current branch clean between iterations.
JRI creates the managed work branch as `ralph/<current-host-branch>` and keeps reset points in local runtime state instead of shared Git tags.

Attempt history is persisted on the invoking branch under `.jri/attempts/<task-slug>.yaml` before JRI clears Ralph's runtime state.

## Generated Structure

`jri init` creates a git repo when needed, then commits project-owned scaffold under `.jri/` and a root `Makefile` when the repo does not already define one.
The `.jri/` scaffold comes from packaged template assets.
The root `Makefile` is generated explicitly in code so JRI can treat repo-root files more cautiously.
The Pi prompt/skill/extension/tools bundle is installation-owned and assembled into a temporary runtime package for each chat or Ralph run.
Alongside the committed scaffold, these `.gitignore`d runtime files are generated as needed:

- `state.json`: active runtime state for sessions, loop tracking, and recovery.
- `state.json.bak`: backup written while updating runtime state.
- `metrics.json`: runtime summary.
- `signals/`: `stop` and `result` files used by active runs.
- `logs/`: Ralph logs, exported sessions, diffs, recovery logs, and timeline data.
- `worktree/`: where Ralph makes changes.
