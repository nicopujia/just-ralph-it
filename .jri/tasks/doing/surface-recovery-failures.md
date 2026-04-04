---
title: Surface recovery failures
priority: 2
assignee: Ralph
depends_on:
  - define-stale-run-recovery-for-jri-start
  - protect-task-state-during-recovery
  - add-structured-status-output
acceptance_criteria:
  - Recovery-time exceptions no longer disappear silently.
  - Recovery failures produce persisted artifacts or status-visible signals.
  - Tests cover at least one failed recovery path and show how operators can inspect it.
---

Phase II forbids silent failure during recovery.

Make recovery-time failures visible enough that operators can understand what failed without reproducing the run manually.
