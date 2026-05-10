# Intent Graph Architecture Implementation

## TL;DR
> **Summary**: Replace draft-task intent handling with a topic-tree Intent Graph, JRI-owned graph tools, deterministic Graph Checker, and an LLM Intent Compiler that emits append-only `todo` tasks only after user-confirmed compilation.
> **Deliverables**:
> - `.jri/graph/**/NODE.md` graph storage and graph tool APIs
> - Graph Checker for structural/tool-conformance validation
> - `compile_graph` Interrogator tool that compiles graph diffs into append-only tasks or fails with blockers
> - Full removal of draft tasks, draft promotion, Interrogator Validator, and `jsonschema`
> - TDD test coverage and docs/prompt updates
> **Effort**: Large
> **Parallel**: YES - 4 waves
> **Critical Path**: Task 1 → Task 2 → Task 4 → Task 7 → Task 10 → Final Verification

## Context
### Original Request
Discuss and plan the best way to implement the architecture in `docs/arch.md`, especially Interrogator memory, task creation, and Ralph execution boundaries.

### Interview Summary
- Replace draft tasks with an Intent Graph under `.jri/graph/`.
- Graph is a topic tree: each directory is a graph node; each node has one `NODE.md`.
- `NODE.md` frontmatter is minimal deterministic metadata: `title`, `state: active|archived`, optional `archive_reason`.
- `NODE.md` body is flexible whiteboard/context memory for questions, answers, assumptions, evidence, rejected paths, scope notes, and QA notes.
- Interrogator edits graph only through JRI-owned graph tools.
- `compile_graph` is called only after Interrogator believes graph is ready and confirms with the user.
- `compile_graph` either emits valid append-only `todo` tasks and commits graph+tasks together, or fails with blockers and creates no commit/tasks.
- Ralph execution remains separate.
- Deprecated draft-era behavior is removed entirely; no backwards compatibility or migration.
- TDD is mandatory.

### Metis Review (gaps addressed)
- Added canonical graph path validation rules and path traversal guardrails.
- Added all-or-nothing graph/tool mutation requirements.
- Added compiler transaction rules for no partial task files and no commit on failure.
- Added security guardrails for YAML parsing, path normalization, symlink rejection, and LLM output validation.
- Added explicit no-scope-creep guardrails: no typed relationships, no redirects, no migration, no graph database, no Ralph auto-start.

## Work Objectives
### Core Objective
Implement the new JRI intent pipeline:

```text
User <-> Interrogator <-> Intent Graph --compile_graph--> todo tasks -> Ralph
```

### Deliverables
- Python domain models and validators for graph nodes, graph metadata, graph paths, and task lifecycle.
- GraphStore-style API for `create_node`, `read_node`, `apply_graph_patch`, `update_node_metadata`, and `move_node`.
- Deterministic Graph Checker integrated into existing checks.
- `compile_graph` Interrogator tool and backing service flow.
- Removal of `draft` task status, draft directories, promotion records, Interrogator Validator semantics, and `jsonschema` dependency.
- Updated docs and agent prompt/tool bundle to describe Intent Graph + Intent Compiler.

### Definition of Done (verifiable conditions with commands)
- `uv run pytest tests/unit/test_graph_paths.py -q` exits `0`.
- `uv run pytest tests/unit/test_graph_store.py -q` exits `0`.
- `uv run pytest tests/unit/test_graph_patch.py -q` exits `0`.
- `uv run pytest tests/unit/test_graph_checker.py -q` exits `0`.
- `uv run pytest tests/integration/test_compile_graph.py -q` exits `0`.
- `uv run pytest tests/integration/test_init.py tests/integration/test_status.py tests/integration/test_promote.py tests/integration/test_loop.py -q` exits `0` after obsolete draft-era assertions are removed/replaced.
- `make check` exits `0`.

### Must Have
- TDD for every task: failing tests first, implementation second, refactor/cleanup third.
- Graph path inputs are semantic paths such as `auth/oauth`, never raw `.jri/graph/.../NODE.md` paths.
- Reject absolute paths, `..`, empty path segments, `NODE.md` in input, symlink traversal, destination collisions, and malformed YAML frontmatter.
- Graph mutations validate fully before writing.
- `apply_graph_patch` is body-only and never mutates frontmatter.
- `compile_graph` cannot edit graph or source code; it can only create valid `todo` tasks through JRI task-creation tools.
- Compiler output is validated as a batch before any task is written.
- If compiler validation or commit fails, emitted task writes are rolled back.
- `compile_graph` success commits graph changes and emitted tasks together; no tag; no Ralph start.

### Must NOT Have
- No draft tasks, `.jri/tasks/draft`, draft-to-todo promotion, draft compatibility, or draft migration.
- No Interrogator Validator phase; semantic ambiguity is a compiler failure.
- No typed frontmatter relationships in first implementation.
- No timestamps/source/provenance metadata in graph frontmatter.
- No Mermaid, symlinks, graph database abstraction, redirects/tombstones, or public graph CRUD CLI.
- No tests for docs or prompts.

## Verification Strategy
> ZERO HUMAN INTERVENTION - all verification is agent-executed.
- Test decision: TDD required with pytest; repo uses `uv` and `make check`.
- QA policy: Every task has agent-executed scenarios.
- Evidence: `.sisyphus/evidence/task-{N}-{slug}.{ext}`

## Execution Strategy
### Parallel Execution Waves
> Target: 5-8 tasks per wave. <3 per wave (except final) = under-splitting.
> Extract shared dependencies as Wave-1 tasks for max parallelism.

Wave 1: Tasks 1-4 foundation: graph models/path validation, graph storage, graph patcher, draft/status removal groundwork.
Wave 2: Tasks 5-8 integrations: graph checker/status, Interrogator graph tools, compiler task writer, compile_graph LLM flow.
Wave 3: Tasks 9-11 cleanup/integration: init/scaffold/docs/prompts, dependency cleanup, end-to-end integration hardening.
Wave 4: Final verification wave F1-F4.

### Dependency Matrix (full, all tasks)
- Task 1 blocks Tasks 2, 3, 5, 6, 8.
- Task 2 blocks Tasks 5, 6, 9.
- Task 3 blocks Task 6.
- Task 4 blocks Tasks 7, 8, 10.
- Task 5 blocks Tasks 8, 9, 10.
- Task 6 blocks Task 10.
- Task 7 blocks Task 8.
- Task 8 blocks Task 10.
- Task 9 blocks Task 10.
- Tasks 1-10 block Task 11 and final verification.

### Agent Dispatch Summary (wave → task count → categories)
- Wave 1 → 4 tasks → quick/unspecified-high
- Wave 2 → 4 tasks → unspecified-high/deep
- Wave 3 → 3 tasks → quick/unspecified-high/writing
- Wave 4 → 4 review tasks → oracle/unspecified-high/deep

## TODOs
> Implementation + Test = ONE task. Never separate.
> EVERY task MUST have: Agent Profile + Parallelization + QA Scenarios.

- [x] 1. Define graph domain models and path validation

  **What to do**: Use TDD to add Python-only graph domain models and validators. Add `GraphNodeMetadata`, `GraphNode`, `GraphPath`, and graph error types in `src/jri/core/models.py` or a new `src/jri/core/graph.py` imported from models. Define canonical semantic paths: relative slash-separated path, no absolute paths, no `..`, no empty segments, no `NODE.md`, no hidden traversal, normalized `/`, safe path resolution under `.jri/graph`. Define node frontmatter schema: required `title: str`, `state: active|archived`, optional `archive_reason: str`; reject unknown keys; `archive_reason` required when archived and cleared/forbidden when active.
  **Must NOT do**: Do not add Pydantic, JSON Schema, UUID IDs, typed graph relationships, timestamps, source metadata, or migration logic.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: foundational validation touches security-sensitive filesystem paths and type models.
  - Skills: [`code-security`] - Path traversal/YAML/file validation guardrails.
  - Omitted: [`frontend-design`] - No UI work.

  **Parallelization**: Can Parallel: NO | Wave 1 | Blocks: 2,3,5,6,8 | Blocked By: []

  **References**:
  - Pattern: `src/jri/core/models.py:5` - current literals/dataclasses live here.
  - Pattern: `src/jri/core/tasks.py:257` - current frontmatter parsing split point.
  - Pattern: `src/jri/core/paths.py:51` - current JRI path helpers include `state.json` path; graph paths should follow this style.
  - Security: code-security path traversal guidance loaded in planning session.

  **Acceptance Criteria**:
  - [ ] Write failing tests first in `tests/unit/test_graph_paths.py` for valid paths, rejected absolute paths, rejected `..`, rejected empty segments, rejected `NODE.md`, rejected symlink escape, metadata unknown-key rejection, archived reason required, active reason cleared/forbidden.
  - [ ] Implement validators until `uv run pytest tests/unit/test_graph_paths.py -q` exits `0`.
  - [ ] No dependency additions in `pyproject.toml`.

  **QA Scenarios**:
  ```
  Scenario: Valid semantic graph path resolves under graph root
    Tool: Bash
    Steps: Run `uv run pytest tests/unit/test_graph_paths.py -q`.
    Expected: Exit code 0; tests prove `auth/oauth` maps to `.jri/graph/auth/oauth/NODE.md`.
    Evidence: .sisyphus/evidence/task-1-graph-paths.txt

  Scenario: Path traversal is rejected
    Tool: Bash
    Steps: Run targeted pytest case for `../outside`, `/tmp/x`, `auth//oauth`, `auth/NODE.md`.
    Expected: Exit code 0; all invalid inputs raise typed validation errors.
    Evidence: .sisyphus/evidence/task-1-path-traversal.txt
  ```

  **Commit**: YES | Message: `feat(graph): add graph path models` | Files: [`src/jri/core/models.py`, `src/jri/core/graph.py`, `tests/unit/test_graph_paths.py`]

- [x] 2. Implement GraphStore create/read/metadata/move APIs

  **What to do**: TDD a GraphStore API for `create_node`, `read_node`, `update_node_metadata`, and `move_node`. `create_node(path,title,body)` creates `.jri/graph/<path>/NODE.md`, auto-creates missing parent nodes with inferred title, `state: active`, and empty body. `read_node(path, depth=1)` returns metadata, full body, and child path/title/state summaries; archived children are title/state only. `update_node_metadata` sets `title`, `state`, `archive_reason` with archive rules. `move_node` moves entire subtree, rejects root moves, destination exists, moving into own subtree, invalid paths, and missing source. Missing destination parents should auto-create parent nodes with empty bodies before moving.
  **Must NOT do**: Do not let metadata edits use raw text patches. Do not leave redirects/tombstones. Do not support deletes in first implementation.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: filesystem mutation with atomicity and validation requirements.
  - Skills: [`code-security`] - Filesystem path/symlink safety.
  - Omitted: [`frontend-design`] - No UI work.

  **Parallelization**: Can Parallel: NO | Wave 1 | Blocks: 5,6,9 | Blocked By: 1

  **References**:
  - Pattern: `src/jri/core/tasks.py:87` - current list/load pattern for task files.
  - Pattern: `src/jri/core/tasks.py:253` - YAML safe dump pattern for frontmatter serialization.
  - Pattern: `tests/conftest.py:145` - temp git repo fixture for filesystem tests.

  **Acceptance Criteria**:
  - [ ] Write failing tests first in `tests/unit/test_graph_store.py` covering create, parent auto-create, read depth, archived children, metadata update, move subtree, destination exists, own-subtree rejection, root rejection, invalid metadata.
  - [ ] Implement GraphStore until `uv run pytest tests/unit/test_graph_store.py -q` exits `0`.
  - [ ] All writes use temp/replace atomicity where applicable and never follow symlinks outside graph root.

  **QA Scenarios**:
  ```
  Scenario: Deep node creation auto-creates parents
    Tool: Bash
    Steps: Run `uv run pytest tests/unit/test_graph_store.py -q`.
    Expected: Exit code 0; test asserts `auth/NODE.md`, `auth/oauth/NODE.md`, and child node exist with expected metadata/body.
    Evidence: .sisyphus/evidence/task-2-create-node.txt

  Scenario: Move subtree rejects dangerous moves
    Tool: Bash
    Steps: Run tests for destination exists and move into own subtree.
    Expected: Exit code 0; source tree remains unchanged on rejected move.
    Evidence: .sisyphus/evidence/task-2-move-node-errors.txt
  ```

  **Commit**: YES | Message: `feat(graph): add graph store tools` | Files: [`src/jri/core/graph.py`, `tests/unit/test_graph_store.py`]

- [x] 3. Implement OpenCode-style apply_graph_patch body editor

  **What to do**: TDD `apply_graph_patch` for graph node body edits only. Use OpenCode-style strict envelope:
  ```text
  *** Begin Graph Patch
  *** Update Node: auth/oauth
  @@ context heading or text
  -old body line
  +new body line
  *** End Graph Patch
  ```
  Support multiple `Update Node` operations atomically. Parse and validate whole patch before writing any body. Reject empty patch, invalid envelope, missing node, hunks touching frontmatter, unmatched context, no-op patches, node creation/deletion/move operations, and malformed paths. Empty final body is allowed. Return per-node summary with changed paths and additions/deletions.
  **Must NOT do**: Do not allow patching frontmatter. Do not add source-code patching. Do not silently create nodes.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: parser/patcher correctness and atomicity.
  - Skills: [`code-security`] - Ensure patch paths cannot escape graph root.
  - Omitted: [`frontend-design`] - No UI work.

  **Parallelization**: Can Parallel: YES | Wave 1 | Blocks: 6 | Blocked By: 1

  **References**:
  - External pattern: `/Users/nicopujia/Desktop/anomalyco/opencode/packages/opencode/src/tool/apply_patch.ts` - tool flow.
  - External pattern: `/Users/nicopujia/Desktop/anomalyco/opencode/packages/opencode/src/patch/index.ts` - parser and validate-before-write design.
  - External tests: `/Users/nicopujia/Desktop/anomalyco/opencode/packages/opencode/test/patch/patch.test.ts` - patch behavior coverage.

  **Acceptance Criteria**:
  - [ ] Write failing tests first in `tests/unit/test_graph_patch.py` covering valid single-node patch, valid multi-node atomic patch, invalid envelope, empty patch, missing node, unmatched context, attempted frontmatter edit, and all-or-nothing failure.
  - [ ] Implement until `uv run pytest tests/unit/test_graph_patch.py -q` exits `0`.

  **QA Scenarios**:
  ```
  Scenario: Multi-node graph patch applies atomically
    Tool: Bash
    Steps: Run `uv run pytest tests/unit/test_graph_patch.py -q`.
    Expected: Exit code 0; both node bodies change only after full patch validation.
    Evidence: .sisyphus/evidence/task-3-graph-patch.txt

  Scenario: Frontmatter edit is rejected
    Tool: Bash
    Steps: Run targeted test where patch context attempts to alter YAML frontmatter.
    Expected: Exit code 0; no file content changes and typed error is returned.
    Evidence: .sisyphus/evidence/task-3-frontmatter-reject.txt
  ```

  **Commit**: YES | Message: `feat(graph): add body patcher` | Files: [`src/jri/core/graph.py`, `tests/unit/test_graph_patch.py`]

- [x] 4. Remove draft lifecycle and jsonschema dependency

  **What to do**: TDD removal of draft-era behavior. Change task lifecycle to `todo -> doing -> done` only. Remove `TaskStatus = "draft"`, `TASK_STATUSES` draft entry, draft directory initialization/validation, draft-to-todo promotion APIs/records, and Interrogator Validator semantics. Remove `jsonschema` from `pyproject.toml` and delete/stop packaging `src/jri/core/schemas/*.json`; replace task/state validation with Python-only validators. Keep `pyyaml`.
  **Must NOT do**: Do not add compatibility shims, migration paths, or legacy aliases. Old draft state must fail clearly.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: broad lifecycle removal affects service/tests/docs.
  - Skills: [`package-management`] - Use `uv`; remove dependency correctly.
  - Omitted: [`frontend-design`] - No UI work.

  **Parallelization**: Can Parallel: YES | Wave 1 | Blocks: 7,8,10 | Blocked By: []

  **References**:
  - Pattern: `src/jri/core/models.py:5` - task status literals currently include draft.
  - Pattern: `src/jri/core/models.py:22` - `TASK_STATUSES` and promoted statuses.
  - Pattern: `src/jri/checks/schema.py:14` - checker currently enumerates draft/todo/doing/done.
  - Pattern: `src/jri/core/tasks.py:31` - JSON Schema-backed validation to replace.
  - Dependency: `pyproject.toml:7` - runtime dependencies include `jsonschema` and `pyyaml`.

  **Acceptance Criteria**:
  - [ ] Write failing tests first proving `draft` status is invalid, `.jri/tasks/draft` is not created by init, draft promotion commands/APIs are removed or fail clearly, and Python validators replace JSON Schema behavior.
  - [ ] `uv run pytest tests/unit/test_tasks.py tests/unit/test_state.py -q` exits `0`.
  - [ ] `uv run pytest tests/integration/test_init.py tests/integration/test_promote.py -q` exits `0` with draft-era assertions removed/replaced.
  - [ ] `pyproject.toml` no longer includes `jsonschema`; `pyyaml` remains.

  **QA Scenarios**:
  ```
  Scenario: Draft lifecycle is gone
    Tool: Bash
    Steps: Run `uv run pytest tests/integration/test_init.py tests/integration/test_promote.py -q`.
    Expected: Exit code 0; no `.jri/tasks/draft` scaffold or draft promotion behavior remains.
    Evidence: .sisyphus/evidence/task-4-no-drafts.txt

  Scenario: Python validators replace JSON Schema
    Tool: Bash
    Steps: Run `uv run pytest tests/unit/test_tasks.py tests/unit/test_state.py -q`.
    Expected: Exit code 0; malformed task/state payloads fail via Python validation.
    Evidence: .sisyphus/evidence/task-4-python-validators.txt
  ```

  **Commit**: YES | Message: `refactor(tasks): remove draft lifecycle` | Files: [`src/jri/core/models.py`, `src/jri/core/tasks.py`, `src/jri/checks/schema.py`, `src/jri/core/schemas/`, `pyproject.toml`, `uv.lock`, `tests/`]

- [x] 5. Add deterministic Graph Checker and status graph stats

  **What to do**: TDD Graph Checker for structural/tool-conformance only. It validates `.jri/graph/` layout, `NODE.md` presence for each graph directory, valid YAML frontmatter, allowed keys only (`title`, `state`, `archive_reason`), state rules, no unexpected files except root `MANIFEST.json` if implemented, no symlink escapes, no malformed paths. Integrate checker into existing repo checks and `jri status` graph stats: active node count, archived node count, malformed graph error if invalid. Do not judge ambiguity or readiness.
  **Must NOT do**: Do not add semantic ambiguity checks, warnings, typed relationships, or compiler readiness into Graph Checker.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: validation integration with CLI/status/checks.
  - Skills: [`code-security`] - Filesystem validation/symlink safety.
  - Omitted: [`frontend-design`] - No UI work.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 8,10 | Blocked By: 1,2

  **References**:
  - Pattern: `src/jri/checks/schema.py:17` - existing repo validation entry point.
  - Pattern: `src/jri/checks/schema.py:44` - task tree validation style to replace/extend.
  - Pattern: `src/jri/cli/main.py:529` - CLI status/help text references runtime state.

  **Acceptance Criteria**:
  - [ ] Write failing tests first in `tests/unit/test_graph_checker.py` for malformed YAML, unknown frontmatter keys, invalid state, missing `NODE.md`, symlink, unexpected files policy, archived missing reason.
  - [ ] Add integration tests for `jri status` graph counts and invalid graph reporting.
  - [ ] `uv run pytest tests/unit/test_graph_checker.py tests/integration/test_status.py -q` exits `0`.

  **QA Scenarios**:
  ```
  Scenario: Valid graph reports active/archived counts
    Tool: Bash
    Steps: Run status integration test with active and archived nodes.
    Expected: Exit code 0; CLI output includes deterministic graph counts.
    Evidence: .sisyphus/evidence/task-5-status-graph-counts.txt

  Scenario: Manual malformed graph is rejected
    Tool: Bash
    Steps: Run `uv run pytest tests/unit/test_graph_checker.py -q`.
    Expected: Exit code 0; malformed YAML and unknown keys raise checker errors.
    Evidence: .sisyphus/evidence/task-5-graph-checker.txt
  ```

  **Commit**: YES | Message: `feat(graph): add graph checker` | Files: [`src/jri/core/graph.py`, `src/jri/checks/schema.py`, `src/jri/cli/main.py`, `tests/unit/test_graph_checker.py`, `tests/integration/test_status.py`]

- [x] 6. Expose graph tools to Interrogator agent bundle

  **What to do**: TDD and implement Interrogator tools for `create_node`, `read_node`, `apply_graph_patch`, `update_node_metadata`, and `move_node` in the existing agent bundle/tool registration system. Tool inputs use semantic paths. Tool outputs are lean: create returns path and auto-created parent paths; read returns metadata/body/child summaries; patch returns changed nodes; metadata returns updated metadata; move returns old/new path and moved subtree count. Update Interrogator prompt to use graph notes as whiteboard memory and to call `compile_graph` only after user confirmation.
  **Must NOT do**: Do not expose raw filesystem paths. Do not let tools edit code/tasks directly. Do not write tests for prompts.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: agent tool contracts and bundle registration.
  - Skills: [`code-security`] - Tool inputs are user/LLM-controlled paths.
  - Omitted: [`frontend-design`] - No UI work.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 10 | Blocked By: 2,3

  **References**:
  - Pattern: `src/jri/core/agents/bundle/interrogator/tools.ts` - Interrogator tool registration surface.
  - Pattern: `src/jri/core/agents/bundle/_shared/tools/_registry.py` - shared tool registry pattern.
  - Pattern: `src/jri/core/agents/bundle/_shared/tools/_validation.py:8` - YAML/frontmatter validation helper style.

  **Acceptance Criteria**:
  - [ ] Write failing tests first for tool schema/input validation and GraphStore invocation using existing agent bundle test patterns.
  - [ ] `uv run pytest tests/unit/test_agent_client_boundaries.py -q` exits `0` or new targeted graph tool tests exit `0`.
  - [ ] Interrogator prompt references Intent Graph and graph tools, not draft tasks or Interrogator Validator.

  **QA Scenarios**:
  ```
  Scenario: Interrogator graph tool rejects raw filesystem path
    Tool: Bash
    Steps: Run targeted graph tool test passing `.jri/graph/auth/NODE.md` instead of `auth`.
    Expected: Exit code 0; tool returns validation error.
    Evidence: .sisyphus/evidence/task-6-graph-tool-paths.txt

  Scenario: apply_graph_patch tool updates body only
    Tool: Bash
    Steps: Run graph tool test invoking patch on a node body and attempted frontmatter edit.
    Expected: Exit code 0; body edit succeeds, frontmatter edit fails.
    Evidence: .sisyphus/evidence/task-6-graph-tool-patch.txt
  ```

  **Commit**: YES | Message: `feat(interrogator): add graph tools` | Files: [`src/jri/core/agents/bundle/interrogator/`, `src/jri/core/agents/bundle/_shared/tools/`, `tests/`]

- [x] 7. Add transactional task creation tools for compiler output

  **What to do**: TDD deterministic task batch creation API used by Intent Compiler. Input is a batch of task specs with existing task metadata plus body: `title`, `priority`, `assignee`, `depends_on`, `acceptance_criteria`, `body`. Validate entire batch before writing any `todo` file. Enforce slug uniqueness, valid dependency slugs, valid assignee, priority range, non-empty acceptance criteria, append-only promoted task rule, and no writes outside `.jri/tasks/todo`. Add rollback on write/validation failure.
  **Must NOT do**: Do not allow compiler to edit existing promoted tasks. Do not create drafts. Do not commit here; commit belongs to compile orchestration.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: append-only task safety and transaction semantics.
  - Skills: [`code-security`] - File path and LLM output validation.
  - Omitted: [`frontend-design`] - No UI work.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: 8 | Blocked By: 4

  **References**:
  - Pattern: `src/jri/core/tasks.py:31` - task metadata validation entry point.
  - Pattern: `src/jri/core/tasks.py:91` - append-only enforcement currently tied to promoted task statuses.
  - Pattern: `src/jri/core/models.py:203` - `TaskMetadata` dataclass.

  **Acceptance Criteria**:
  - [ ] Write failing tests first in `tests/unit/test_task_batch_writer.py` for valid batch, duplicate slug, invalid dependency, invalid priority, missing acceptance criteria, write failure rollback, append-only existing task rejection.
  - [ ] `uv run pytest tests/unit/test_task_batch_writer.py -q` exits `0`.

  **QA Scenarios**:
  ```
  Scenario: Valid compiler task batch writes todo tasks
    Tool: Bash
    Steps: Run `uv run pytest tests/unit/test_task_batch_writer.py -q`.
    Expected: Exit code 0; all task files are created only after batch validation.
    Evidence: .sisyphus/evidence/task-7-task-batch-success.txt

  Scenario: Invalid batch writes no partial tasks
    Tool: Bash
    Steps: Run rollback test with one valid and one invalid task spec.
    Expected: Exit code 0; no task file remains after failure.
    Evidence: .sisyphus/evidence/task-7-task-batch-rollback.txt
  ```

  **Commit**: YES | Message: `feat(tasks): add transactional task writer` | Files: [`src/jri/core/tasks.py`, `src/jri/core/models.py`, `tests/unit/test_task_batch_writer.py`]

- [x] 8. Implement compile_graph orchestration and LLM compiler guardrails

  **What to do**: TDD `compile_graph` tool/service. It runs Graph Checker, builds graph diff/context bundle from uncommitted graph changes, invokes LLM Intent Compiler, lets compiler read graph/tasks and code on demand through read-only tools, validates returned task batch, writes tasks transactionally, then commits graph changes and tasks together. On compiler ambiguity or validation failure, return `{exit_code: "fail", errors:[...]}` and leave graph edits uncommitted with no task writes. On commit failure, rollback emitted tasks and return failure. Return success shape with task slugs and commit hash. Do not tag. Do not start Ralph.
  **Must NOT do**: Do not let compiler edit graph/code. Do not persist compile failure reports. Do not create partial tasks. Do not ask user directly.

  **Recommended Agent Profile**:
  - Category: `deep` - Reason: orchestration across LLM, git, graph, task writer, and failure rollback.
  - Skills: [`code-security`] - LLM output and git/file boundaries.
  - Omitted: [`frontend-design`] - No UI work.

  **Parallelization**: Can Parallel: NO | Wave 2 | Blocks: 10 | Blocked By: 5,7

  **References**:
  - Pattern: `src/jri/core/service.py` - central orchestration service.
  - Pattern: `src/jri/core/agents/client.py` - existing Pi/agent execution and result parsing.
  - Pattern: `src/jri/core/git.py` - git commit/status behavior.
  - Pattern: `tests/integration/test_loop.py` - integration patterns for task execution/git state.

  **Acceptance Criteria**:
  - [ ] Write failing integration tests first in `tests/integration/test_compile_graph.py` for success commit, ambiguity failure no commit, invalid compiler output rollback, git commit failure rollback, no Ralph start.
  - [ ] Implement until `uv run pytest tests/integration/test_compile_graph.py -q` exits `0`.
  - [ ] Compiler failure errors include location/node path, ambiguous area, plausible interpretations, and user-facing draft question.

  **QA Scenarios**:
  ```
  Scenario: compile_graph success commits graph and tasks together
    Tool: Bash
    Steps: Run success integration test with fake compiler returning two tasks.
    Expected: Exit code 0; one commit contains graph NODE.md changes and both todo tasks; no tag; Ralph not started.
    Evidence: .sisyphus/evidence/task-8-compile-success.txt

  Scenario: compile_graph ambiguity fails with no commit/tasks
    Tool: Bash
    Steps: Run ambiguity integration test with fake compiler blocker.
    Expected: Exit code 0; return `exit_code=fail`, blockers present, no new commit, no todo files.
    Evidence: .sisyphus/evidence/task-8-compile-fail.txt
  ```

  **Commit**: YES | Message: `feat(compiler): add compile graph flow` | Files: [`src/jri/core/service.py`, `src/jri/core/agents/`, `src/jri/core/tasks.py`, `tests/integration/test_compile_graph.py`]

- [x] 9. Update init/scaffold/status for Intent Graph

  **What to do**: TDD updates to `jri init`, generated `.jri` structure, `.jri/.gitignore`, and status. Init should create `.jri/graph/` root and no `.jri/tasks/draft`. Decide and implement root graph behavior: `.jri/graph/` may be empty initially; no root `NODE.md` required. Status shows graph counts and active task state. Ensure runtime-only `.jri` files remain gitignored as before.
  **Must NOT do**: Do not add public graph CRUD CLI beyond status. Do not add Mermaid/report commands.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: init scaffold and integration behavior.
  - Skills: [] - Existing test patterns sufficient.
  - Omitted: [`frontend-design`] - No UI work.

  **Parallelization**: Can Parallel: YES | Wave 3 | Blocks: 10 | Blocked By: 2,5

  **References**:
  - Pattern: `docs/arch.md:55` - generated structure documentation.
  - Pattern: `src/jri/core/service.py:982` - current `.jri/.gitignore` content.
  - Test: `tests/integration/test_init.py` - scaffold generation coverage.

  **Acceptance Criteria**:
  - [ ] Write failing tests first updating `tests/integration/test_init.py` for `.jri/graph/`, no draft dir, existing runtime gitignore behavior.
  - [ ] Update `tests/integration/test_status.py` for graph stats.
  - [ ] `uv run pytest tests/integration/test_init.py tests/integration/test_status.py -q` exits `0`.

  **QA Scenarios**:
  ```
  Scenario: init creates graph scaffold without draft tasks
    Tool: Bash
    Steps: Run `uv run pytest tests/integration/test_init.py -q`.
    Expected: Exit code 0; `.jri/graph/` exists and `.jri/tasks/draft` does not.
    Evidence: .sisyphus/evidence/task-9-init-graph.txt

  Scenario: status reports graph counts
    Tool: Bash
    Steps: Run `uv run pytest tests/integration/test_status.py -q`.
    Expected: Exit code 0; status includes active/archived graph node counts.
    Evidence: .sisyphus/evidence/task-9-status.txt
  ```

  **Commit**: YES | Message: `feat(init): scaffold intent graph` | Files: [`src/jri/core/service.py`, `src/jri/cli/main.py`, `tests/integration/test_init.py`, `tests/integration/test_status.py`]

- [x] 10. Update docs, prompts, and agent architecture text

  **What to do**: Update concise docs and prompt/tool descriptions to the new architecture. `docs/arch.md` should describe `User <-> Interrogator <-> Intent Graph --compile_graph--> Tasks -> Ralph`, topic-tree graph, Graph Checker, Intent Compiler, no drafts, `todo -> doing -> done`, compile success/failure commit behavior, and Ralph separate execution. Fix stale prompt links to current bundle paths. Update Interrogator prompt to use graph tools and to confirm with user before `compile_graph`. Update Ralph prompt only if it references drafts/promotion. Do not write tests for docs/prompts.
  **Must NOT do**: Do not over-document CLI help in Markdown if `docs/contrib.md` says CLI docs belong in help text. Do not add tests for docs/prompts.

  **Recommended Agent Profile**:
  - Category: `writing` - Reason: docs and prompt prose updates.
  - Skills: [] - No external docs needed.
  - Omitted: [`code-security`] - No code in this task beyond prompt/docs.

  **Parallelization**: Can Parallel: YES | Wave 3 | Blocks: 11 | Blocked By: 4,6,8,9

  **References**:
  - Source: `docs/arch.md:3` - current architecture statement.
  - Source: `docs/arch.md:40` - old draft lifecycle text to replace.
  - Source: `docs/contrib.md:38` - docs update guidance.
  - Prompt path finding: current prompts live under `src/jri/core/agents/bundle/*/prompt.md`, not `src/jri/core/agents/prompts/`.

  **Acceptance Criteria**:
  - [ ] Docs mention no draft tasks and new lifecycle `todo -> doing -> done`.
  - [ ] Docs mention compile failure creates no commit/tasks; compile success commits graph+tasks, no tag, no Ralph start.
  - [ ] Prompts do not reference Interrogator Validator or draft-to-todo promotion.
  - [ ] No doc/prompt tests are added.

  **QA Scenarios**:
  ```
  Scenario: Deprecated terms removed from docs/prompts
    Tool: Bash
    Steps: Run content search for `draft -> todo`, `Interrogator Validator`, and stale prompt path references.
    Expected: Search finds no obsolete architecture references except changelog-like explanatory removal notes if intentionally present.
    Evidence: .sisyphus/evidence/task-10-docs-obsolete-terms.txt

  Scenario: Architecture docs describe compile_graph flow
    Tool: Bash
    Steps: Read `docs/arch.md` and relevant prompt files.
    Expected: Docs/prompt mention Intent Graph, Graph Checker, Intent Compiler, and user confirmation before compile.
    Evidence: .sisyphus/evidence/task-10-docs-flow.txt
  ```

  **Commit**: YES | Message: `docs: describe intent graph architecture` | Files: [`docs/arch.md`, `src/jri/core/agents/bundle/*/prompt.md`, `src/jri/core/agents/bundle/*/tools.ts`]

- [ ] 11. End-to-end cleanup, dependency lock, and full regression

  **What to do**: Finish integration cleanup after all architectural changes. Remove obsolete tests/fixtures tied to draft tasks or rewrite them to graph/compiler behavior. Ensure `uv.lock` reflects removed `jsonschema`. Run targeted suites, then `make check`. Fix any regressions. Ensure no generated artifacts are accidentally committed except intended source/test/docs changes.
  **Must NOT do**: Do not skip failing legacy tests by deleting meaningful coverage; replace draft coverage with graph/compiler coverage.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` - Reason: cross-cutting regression cleanup.
  - Skills: [`package-management`] - Use `uv`; update lock correctly.
  - Omitted: [`frontend-design`] - No UI work.

  **Parallelization**: Can Parallel: NO | Wave 3 | Blocks: Final Verification | Blocked By: 1,2,3,4,5,6,7,8,9,10

  **References**:
  - Commands: `docs/contrib.md:10` - `make check` and `make coverage` guidance.
  - Dependency: `pyproject.toml:7` - runtime dependencies.
  - Tests: `tests/integration/test_loop.py` - broad lifecycle/attempt coverage.

  **Acceptance Criteria**:
  - [ ] `uv run pytest tests/unit/test_graph_paths.py tests/unit/test_graph_store.py tests/unit/test_graph_patch.py tests/unit/test_graph_checker.py tests/unit/test_task_batch_writer.py -q` exits `0`.
  - [ ] `uv run pytest tests/integration/test_compile_graph.py tests/integration/test_init.py tests/integration/test_status.py tests/integration/test_loop.py -q` exits `0`.
  - [ ] `make check` exits `0`.
  - [ ] `pyproject.toml` and `uv.lock` no longer include `jsonschema`; `pyyaml` remains.

  **QA Scenarios**:
  ```
  Scenario: Full unit/integration regression passes
    Tool: Bash
    Steps: Run targeted unit and integration pytest commands listed in acceptance criteria.
    Expected: Exit code 0 for each command.
    Evidence: .sisyphus/evidence/task-11-regression.txt

  Scenario: Project check passes
    Tool: Bash
    Steps: Run `make check`.
    Expected: Exit code 0.
    Evidence: .sisyphus/evidence/task-11-make-check.txt
  ```

  **Commit**: YES | Message: `test: verify intent graph architecture` | Files: [`tests/`, `pyproject.toml`, `uv.lock`]

## Final Verification Wave (MANDATORY — after ALL implementation tasks)
> 4 review agents run in PARALLEL. ALL must APPROVE. Present consolidated results to user and get explicit "okay" before completing.
> **Do NOT auto-proceed after verification. Wait for user's explicit approval before marking work complete.**
> **Never mark F1-F4 as checked before getting user's okay.** Rejection or user feedback -> fix -> re-run -> present again -> wait for okay.
- [ ] F1. Plan Compliance Audit — oracle
  - Verify no draft-era compatibility remains, all required graph/compiler deliverables exist, and compile semantics match this plan.
- [ ] F2. Code Quality Review — unspecified-high
  - Inspect graph tools, checker, compiler transaction boundaries, tests, and docs for maintainability and duplication.
- [ ] F3. Real Manual QA — unspecified-high
  - Execute `jri init`, graph tool operations, `compile_graph` success/failure fixtures, `jri status`, and `make check`; collect evidence.
- [ ] F4. Scope Fidelity Check — deep
  - Verify no out-of-scope features were added: typed relationships, migration, Mermaid, public graph CRUD CLI, tags, Ralph auto-start, Pydantic, JSON Schema.

## Commit Strategy
- Commit after each task using lowercase concise messages matching repo guidance.
- Do not push.
- Do not amend unless hooks modify files after a commit created in this session and git safety rules allow it.
- Each implementation task commits its source/test/docs changes only after its acceptance commands pass.

## Success Criteria
- New JRI flow is implemented: Interrogator graph tools → graph checker → `compile_graph` → append-only `todo` tasks → Ralph separately.
- Draft task lifecycle and Interrogator Validator are removed completely.
- TDD coverage exists for graph paths, graph store/tools, graph patcher, checker, compiler, task writer, lifecycle removal, and init/status.
- `make check` passes.
