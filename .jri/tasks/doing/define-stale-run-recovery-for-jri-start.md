---
title: Define stale-run recovery for jri start
priority: 1
assignee: Ralph
depends_on:
  - implement-crash-safe-state-storage
acceptance_criteria:
  - `jri start` documents and implements the agreed recovery behavior for interrupted work.
  - When `jri start` finds a stale `doing` task with a dead tracked process, it moves the task back to `todo`, resets loop state as documented, and records the recovery.
  - The system can recover from stale `doing` state and stale process metadata without manual file surgery.
  - Tests cover restart after interruption for both clean and stale-process scenarios, including foreground and detached runs.
---

Phase II requires resumable execution and reliable control.

Define and implement the recovery model for foreground and detached runs, including what `jri start` does when the tracked process is gone but task state still says work is in progress.
