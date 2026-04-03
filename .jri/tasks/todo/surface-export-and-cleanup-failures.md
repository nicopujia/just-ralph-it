---
title: Surface export and cleanup failures
priority: 2
assignee: Ralph
depends_on:
  - add-execution-timeline
  - add-structured-status-output
  - harden-per-task-logs
acceptance_criteria:
  - Best-effort export or cleanup failures are no longer silently ignored.
  - Export and cleanup failures appear in task logs, timeline artifacts, or status-visible signals.
  - Tests cover at least one previously silent export or cleanup failure path.
---

Phase II forbids silent failure outside the main task body too.

Make export and cleanup problems visible even when the primary task execution already finished.
