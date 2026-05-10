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
