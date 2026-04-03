---
title: Show blocked and human work in status
priority: 2
assignee: Ralph
depends_on:
  - adopt-phase-ii-outcome-model
acceptance_criteria:
  - `jri status` clearly shows counts by state.
  - `jri status` clearly shows blocked tasks.
  - `jri status` clearly shows human-required tasks.
  - Tests verify the output for representative blocked and human-required cases.
---

Phase II's example status surface must expose the system's actionable backlog, not just generic counts.

Add a human-readable status presentation that makes blockers and human escalations obvious without reading raw logs.
