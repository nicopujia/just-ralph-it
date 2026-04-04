---
title: Show needs-human work in status
priority: 2
assignee: Ralph
depends_on:
  - adopt-phase-ii-outcome-model
  - define-needs-human-representation
  - apply-failed-retry-and-escalation-policy
acceptance_criteria:
  - `jri status` clearly shows counts by state.
  - `jri status` clearly shows tasks that need human input.
  - Tests verify the output for representative needs-human cases.
---

Phase II's example status surface must expose the system's actionable backlog, not just generic counts.

Add a human-readable status presentation that makes needs-human work obvious without reading raw logs.
