---
title: Eliminate silent failure paths
priority: 2
assignee: Ralph
depends_on:
  - adopt-phase-ii-outcome-model
  - add-execution-timeline
acceptance_criteria:
  - All known failure paths produce inspectable artifacts or status-visible signals.
  - Suppressed best-effort failures are either removed or surfaced explicitly.
  - Tests cover at least one previously silent failure path and verify it becomes understandable.
---

Phase II explicitly forbids silent failure.

Audit the loop for swallowed exceptions, best-effort cleanup failures, and ambiguous fallback outcomes, then make every failure mode visible enough for a human to understand what happened.
