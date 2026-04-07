---
title: Add jri- prefix to .opencode/ files
priority: 2
assignee: Ralph
depends_on: []
acceptance_criteria:
  - "`jri init` creates `.opencode/agents/jri-interrogator.md`, `.opencode/agents/jri-ralph.md`, and `.opencode/tools/jri-result.js` (old names no longer used)"
  - "`jri upgrade` on an existing project renames old files to new prefixed names and commits the change"
  - "`.gitignore` references the new prefixed filenames instead of old ones"
  - "Running `jri chat` after `jri init` or `jri upgrade` works correctly with the new agent/tool filenames"
  - "All existing tests pass"
  - "`make check` passes"
---

Rename JRI-managed files inside `.opencode/` to have a `jri-` prefix, making it clear they're managed by JRI and not user-editable.

## Files to rename

- `.opencode/agents/interrogator.md` → `.opencode/agents/jri-interrogator.md`
- `.opencode/agents/ralph.md` → `.opencode/agents/jri-ralph.md`
- `.opencode/tools/result.js` → `.opencode/tools/jri-result.js`

## Implementation

1. Update `src/jri/core/service.py`:
   - Rename `_MANAGED_AGENT_FILENAMES` constants to include `jri-` prefix
   - Rename `_MANAGED_TOOL_FILENAMES` constants to include `jri-` prefix
   - Update `_INIT_COMMIT_PATHS` and `_UPGRADE_COMMIT_PATHS` to use new filenames

2. Update `.gitignore`:
   - Replace old paths with new prefixed paths in the JRI-managed section

3. Ensure `jri init` creates files with the new names

4. Ensure `jri upgrade` renames existing files:
   - Detect old filenames
   - Rename them to new filenames
   - Commit the rename

## Notes

- `opencode.json` is NOT renamed (it's a config file, not an agent/tool)
- `.gitignore` is NOT renamed (it's modified, not created by JRI)
- The `.opencode/` directory itself is NOT renamed (only files inside it)
- Existing projects should be upgraded automatically via `jri upgrade`