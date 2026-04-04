---
title: Enforce loop execution bounds
priority: 2
assignee: Ralph
depends_on:
  - define-stale-run-recovery-for-jri-start
acceptance_criteria:
  - The CLI supports the agreed Phase II iteration and per-task timeout controls.
  - Tests cover iteration-limit and per-task-timeout behavior.
  - When execution stops because of a configured bound, that reason is visible to operators.
---

Phase II control requires explicit loop boundaries.

Implement the agreed execution bounds so the loop stops predictably and explains why it stopped when configured limits are reached.
