---
title: Add --fresh flag to jri chat
priority: 2
assignee: Ralph
depends_on: []
acceptance_criteria:
  - "`jri chat --help` documents the `--fresh` flag"
  - "`jri chat --fresh` clears the session ID from `.jri/state.json` before launching OpenCode"
  - "`jri chat` without `--fresh` reuses the saved session ID (existing behavior unchanged)"
  - "After `jri chat --fresh`, a new session ID is saved to `.jri/state.json`"
  - "All existing tests pass"
  - "`make check` passes"
---

Add a `--fresh` flag to `jri chat` that starts a new OpenCode session instead of reusing the saved one.

## Current behavior

`jri chat` currently reuses the saved OpenCode session ID from `.jri/state.json` if available. This is useful for continuing conversations, but sometimes users want to start fresh.

## New behavior

- `jri chat`: Reuses saved session (existing behavior)
- `jri chat --fresh`: Clears the session ID from state and starts a new OpenCode session

## Implementation

1. Add `--fresh` flag to the `chat` subparser in `src/jri/cli/main.py`

2. Update `JriService.chat()` in `src/jri/core/service.py`:
   - Accept a `fresh: bool` parameter
   - If `fresh=True`, clear the session ID from state before launching chat
   - The old session is abandoned (not deleted) — just forgotten from JRI's perspective

3. Update tests to verify:
   - `jri chat --fresh` clears the session ID
   - `jri chat` without `--fresh` reuses the session ID
   - After `--fresh`, a new session ID is saved

## Notes

- The old OpenCode session is NOT deleted — it remains in OpenCode's storage
- This is equivalent to manually running `jri state clear-session` followed by `jri chat`
- The flag name is `--fresh` not `--new` for clarity