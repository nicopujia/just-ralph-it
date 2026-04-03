---
title: Add reliable resume and stale-run recovery
priority: 1
assignee: Ralph
depends_on:
  - implement-crash-safe-state-storage
acceptance_criteria:
  - A user-facing resume flow exists and is documented.
  - The system can recover from stale `doing` state and stale process metadata without manual file surgery.
  - Tests cover resuming after interruption for both clean and stale-process scenarios.
  - `stop`, `halt`, and `resume` semantics are consistent and inspectable.
---

Phase II requires resumable execution and reliable control.

Define and implement the resume model for foreground and detached runs, including what happens when the tracked process is gone but task state still says work is in progress.
