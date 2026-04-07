---
title: Investigate "Session not found" error after task completion
priority: 2
assignee: Ralph
depends_on: []
acceptance_criteria:
  - "Root cause of 'Session not found' error is identified and documented in the task file"
  - "If reproducible: fix is implemented and tested"
  - "If not reproducible: investigation findings are documented and monitoring/logging is improved"
  - "All existing tests pass"
  - "`make check` passes"
---

Investigate and fix a bug where `jri start` completes a task successfully but then reports "Error: Session not found".

## Bug report

**When**: After Ralph finishes and reports task completion
**Where**: Running `jri start` on `~/gupta-to-web`
**Error**: "Error: Session not found"
**Frequency**: Unknown (happened once, may be reproducible)

## Investigation needed

1. Identify where the "Session not found" error originates:
   - Is it from OpenCode CLI?
   - Is it from JRI's session management code?
   - Is it from state management?

2. Determine the root cause:
   - Is the session ID being cleared prematurely?
   - Is there a race condition between task completion and session cleanup?
   - Is the session ID invalid or corrupted?

3. Find the reproduction steps:
   - Can it be reproduced on the same project?
   - Does it happen on other projects?
   - What conditions trigger it?

## Possible causes

- Session ID cleared before final state save
- OpenCode session expired or cleaned up during task execution
- State corruption during concurrent access
- Error handling path that clears session inappropriately

## Files to investigate

- `src/jri/core/service.py` (session management in `start()` and `_run_iteration()`)
- `src/jri/core/state.py` (session ID storage)
- `src/jri/core/opencode.py` (OpenCode client session handling)
- `.jri/state.json` (check for corruption patterns)