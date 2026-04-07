---
title: Centralize all git commit messages in git.py
priority: 2
assignee: Ralph
depends_on: []
acceptance_criteria: []
---

Investigate all places where `jri` makes git commits and ensure all commit messages flow through constants defined in `src/jri/core/git.py`.

## Current state

`src/jri/core/git.py` already defines commit message constants:
- `MSG_INIT`
- `MSG_UPGRADE`
- `MSG_UPGRADE_AUTO`
- `MSG_START_BEGIN`
- `MSG_START_COMPLETE`
- `MSG_PROMOTE`
- `MSG_RECOVER_FAILED`
- `MSG_RECOVER_STALE`
- `MSG_RECOVER_NEEDS_HUMAN`
- `MSG_ESCALATE_FAILED`
- `MSG_ESCALATE_HUMAN`
- `MSG_RALPH_FINALIZE`
- `MSG_RALPH_PARTIAL`

## Goal

Find ALL places in the codebase where `git.commit()` or similar methods are called, and ensure they use these constants (or new constants if needed) rather than inline strings.

## Implementation

1. Search the codebase for all git commit calls:
   - `git.commit(...)`
   - `repo.commit(...)`
   - Any subprocess calls to `git commit`

2. For each commit call:
   - If it uses a constant from `git.py`, verify it's the right one
   - If it uses an inline string, create a new constant or use an existing one
   - Update the call to use the constant

3. Add any new constants needed for previously-uncentralized commits

4. Add tests to verify all commits use constants (optional but recommended)

## Files to check

- `src/jri/core/service.py` (init, upgrade, promote, start, recovery)
- `src/jri/cli/main.py` (any CLI-level commits)
- Any other files that might make git commits

## Notes

- This is a refactoring task — no behavior change
- All existing tests should pass
- The goal is maintainability: if we ever need to change commit message format, it's in one place