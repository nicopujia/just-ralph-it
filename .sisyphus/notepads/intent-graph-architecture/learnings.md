## 2026-05-10 Task: task-1-exploration
- Domain model style: use `@dataclass(frozen=True)` and `Literal` aliases in `src/jri/core/models.py`.
- Path helpers pattern: centralize filesystem layout in `src/jri/core/paths.py`; service resolves repo root early.
- YAML/frontmatter parsing pattern: `src/jri/core/tasks.py` has `_split_frontmatter`, `_find_frontmatter_boundary`, and human-readable `ValueError` style.
- Validation layering: low-level helpers raise `ValueError`; service boundaries wrap in `JriError`.
- Filesystem safety references: agent shared tool validation rejects symlinks/path escapes; reuse or mirror that pattern.
- Atomic persistence reference: `src/jri/core/state.py` uses temp/replace style writes.
- Test placement: Task 1 unit tests should be new `tests/unit/test_graph_paths.py`.
- Test style: use `tmp_path` for pure path/metadata tests; use `git_repo` only if real repo root/symlink behavior requires it.
- Test assertions: behavior-focused snake_case names; use `pytest.raises(..., match="...")` with stable substrings.

## 2026-05-10 Task: define-graph-domain-models
- Graph semantic path helpers now live in `src/jri/core/graph.py`; path layout is exposed through `JriPaths.graph_dir` and `JriPaths.graph_node_path`.
- Graph metadata follows existing domain style with frozen dataclasses and a narrow `GraphNodeState = Literal["active", "archived"]` alias.
- Active node metadata normalizes empty `archive_reason` to `None`, while non-empty active archive reasons are rejected and archived nodes require a non-empty reason.

## 2026-05-10 Task: graph-validation-review-fix
- Graph semantic paths explicitly reject backslashes with a stable slash-separated error before segment validation.
- Node frontmatter parsing wraps `yaml.YAMLError` as `ValueError("invalid node metadata YAML")` so malformed YAML does not leak parser internals.

## 2026-05-10 Task: implement-graph-store-apis
- GraphStore lives in `src/jri/core/graph.py` alongside Task 1 helpers and reuses `validate_graph_path`, `graph_node_path`, and `validate_node_metadata` for all public operations.
- Graph node writes use YAML `safe_dump` with `sort_keys=False` and temp-file `os.replace` persistence patterned after `StateStore`.
- Graph reads flatten child summaries by depth and stop descent at archived children so archived subtrees expose only the archived child title/state summary.

## 2026-05-10 Task: implement-graph-apply-patch
- Graph body patches use a graph-specific envelope, `*** Update Node: <semantic-path>` operations, and body-only hunks; they intentionally reject file-style add/delete/move operations.
- `apply_graph_patch` validates paths, nodes, hunk matches, no-op status, and frontmatter markers before calling `GraphStore._write_node`, preserving all-or-nothing behavior across multiple nodes.
- Non-empty patched bodies are normalized to a trailing newline, while deleting the final body line produces an allowed empty body.

## 2026-05-10 Task: remove-draft-lifecycle-jsonschema
- Task status vocabulary is now `todo`, `doing`, `done`; `.jri/tasks/draft` is not scaffolded by init.
- Task and state validation now lives in Python validators in `src/jri/core/tasks.py`; JSON Schema resources and `jsonschema` are no longer runtime dependencies.
- Draft promotion service APIs and promotion records were removed; legacy promotion tool entrypoints now fail with a clear removal message.

## 2026-05-10 Task: graph-apply-patch-count-fix
- Graph patch summary counts now track parsed `+` and `-` hunk lines directly, which avoids duplicate shared-line ambiguity and matches the user-visible patch text.

## 2026-05-10 Task: task-4-verification-cleanup
- Interrogator chat no longer carries an Interrogator Validator model or runtime resource; chat presets only resolve interrogator and explore models.
- Interrogator and Ralph Validator prompts now describe direct todo-task readiness rather than draft promotion or validator approval workflows.
- Live and self-hosting proof tests should create executable todo tasks directly; promotion API names only appear in removal tests via constructed strings to keep stale-reference sweeps clean.

## 2026-05-10 Task: task-4-loop-test-cleanup
- Legacy loop tests that used draft follow-ups now create additive todo follow-up tasks and assert the original task still completes.
- Dirty-workdir loop coverage now uses current lifecycle task edits and unrelated files: non-force aborts on dirty todo changes, while force stashes mixed dirty work.
- Malformed task wrapping should target a tracked lifecycle directory such as `todo`, not a removed draft path.

## 2026-05-10 Task: transactional-task-batch-writer
- Compiler task batches use `CompilerTaskSpec` in `src/jri/core/models.py` and derive deterministic slugs from titles using the same slug shape as the existing upsert task tool.
- `create_task_batch` validates duplicate slugs, existing promoted task collisions, dependency closure, acceptance criteria, metadata, body, and literal `.jri/tasks/todo` paths before writing any task file.
- Batch writes are rollback-protected by tracking candidate paths before each write, so write failures remove newly created files without touching pre-existing promoted tasks.

## 2026-05-10 Task: deterministic-graph-checker-status
- Graph structural checks live in `src/jri/core/graph.py` as `check_graph_tree`/`validate_graph_tree`, returning deterministic sorted errors plus active/archived counts.
- `jri status` reports graph counts without failing the command, while repository schema validation treats graph checker errors as validation failures.
- Root `.jri/graph/` may be missing or empty; `MANIFEST.json` is the only tolerated root file besides node directories.

## 2026-05-10 Task: expose-graph-tools-to-interrogator
- Interrogator graph tools are shared Python handlers under `src/jri/core/agents/bundle/_shared/tools/graph.py`, registered with hyphenated Pi tool names that match existing bundle conventions.
- Agent graph tool outputs intentionally expose only semantic paths and lean JSON summaries: create parents, read metadata/body/children, patch changed nodes, metadata update payload, and move subtree count.
- The Interrogator prompt now treats the Intent Graph as whiteboard memory and requires explicit user confirmation before `compile_graph`.

## 2026-05-10 Task: compile-graph-orchestration
- `JriService.compile_graph` uses a runtime seam named `compile_intent_graph(root=..., context=...)`; tests can fake this seam without invoking Pi or Ralph.
- Compiler context should expose semantic graph paths plus parsed node metadata/body, not raw `.jri/graph/**/NODE.md` filesystem paths.
- Compile commits should be scoped to changed graph node files plus emitted todo task files; commit failures must unlink emitted tasks and unstage those paths while leaving graph edits uncommitted.

## 2026-05-10 Task: compile-graph-production-surfaces
- Interrogator tools expose Python handlers through both `_shared/tools/_registry.py` and `interrogator/tools.ts`; new tools also need `__init__.__all__` coverage because tests assert the public handler surface.
- `PiRuntime.compile_intent_graph` uses the existing RPC prompt stream but starts Pi with `--no-extensions --no-skills --no-prompt-templates --no-context-files --tools read,grep,find,ls` so the compiler has read-only access.
- `launch_chat` maintains a managed `--tools` allowlist; adding an Interrogator tool requires updating the launch command expectation in `tests/unit/test_pi.py`.

## 2026-05-10 Task: update-init-scaffold-status-for-intent-graph
- `jri init` creates `.jri/graph/` as an empty runtime directory without a root `NODE.md`; Git will not preserve that empty directory in commits, so scaffold must create it locally.
- Keep `.jri/graph/` root empty on init because the graph checker only tolerates root node directories and `MANIFEST.json`.
- `jri status` can rely on `check_graph_tree` for empty or missing graph roots; it reports `Graph: 0 active, 0 archived` without extra compatibility logic.

## 2026-05-10 Task: update-docs-prompts-agent-architecture-text
- Public architecture docs should describe the current flow as `User <-> Interrogator <-> Intent Graph --compile_graph--> Tasks -> Ralph`, with `compile-graph` as the Interrogator tool name.
- Compile success commits graph changes and emitted todo tasks together without starting Ralph or creating a tag; compile failure leaves no commit and no emitted tasks.

## 2026-05-10 Task: end-to-end-cleanup-regression
- Final cleanup kept `pyyaml` as the only runtime dependency in `pyproject.toml`/`uv.lock`; `jsonschema` is absent from project and lock files.
- Strict typecheck after compile-graph required explicit casts for list payload validation and protocol-compatible fake runtimes; `JriService` accepts injected runtimes as external test doubles while storing them as `AgentRuntime`.
- Obsolete draft-era coverage-gate assertions should target current todo task writes, removed tool registry errors, and the three-state `todo`, `doing`, `done` status vocabulary.

## 2026-05-10 Task: task-11-review-stale-reference-fix
- Schema validation coverage now exercises valid `todo`, `doing`, and `done` task directories instead of accepting `.jri/tasks/draft` compatibility.
- Self-hosting proof terminology now describes compiling intent into executable todo tasks, not draft-to-todo promotion.
- Private append-only task terminology was renamed from promoted-task wording to lifecycle/tracked wording; remaining stale-search hits are intentional removal tests, invalid draft-state rejection tests, init no-draft scaffolding coverage, or the compiler `draft_question` field.

## 2026-05-09 Task: final-verification-reject-fixes
- Interrogator direct todo creation exposure is removed from both the TypeScript chat tool registration and the managed `jri chat` Pi tool allowlist; task-writing handlers remain only on the shared dispatch surface for non-Interrogator coverage.
- Graph node persistence now uses exclusive per-write temp files in the target node directory, so a pre-existing `.NODE.md.tmp` symlink is not followed or overwritten.
- Multi-node graph patches roll back previously written nodes if a later write fails, and move failures clean up newly auto-created destination parent nodes while preserving the source subtree.

## 2026-05-09 Task: final-rollback-gap-fixes
- Graph patch rollback now records each original node before attempting its write, covering failures that occur after the current node replacement but before `write_node` returns.
- Destination parent auto-creation now cleans up already-created parents if a later parent write fails before `move_node` reaches the subtree replace.
- Regression coverage forces post-replacement graph patch failure and partway parent-creation failure, asserting original graph bodies and source subtrees are restored.
