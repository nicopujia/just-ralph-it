---
title: Protect task state during recovery
priority: 1
assignee: Ralph
depends_on:
  - implement-crash-safe-state-storage
  - define-stale-run-recovery-for-jri-start
acceptance_criteria:
  - Recovery paths do not silently swallow task-state corruption or leave ambiguous partial moves.
  - Recovery failures are persisted in inspectable artifacts.
  - Tests cover at least one recovery failure path and verify the resulting state is understandable.
---

Phase II requires no task corruption.

Audit and harden recovery paths so task files remain trustworthy even when a run fails mid-transition or secondary cleanup steps fail.
