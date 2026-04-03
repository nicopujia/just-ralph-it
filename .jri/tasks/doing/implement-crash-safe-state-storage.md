---
title: Make state storage crash-safe
priority: 1
assignee: Ralph
depends_on:
  - phase-1-2-quality-gate-foundation
acceptance_criteria:
  - State writes use a crash-safe persistence strategy rather than direct overwrite only.
  - Simulated interrupted-write tests verify the system can recover without leaving unreadable state as the only copy.
  - Recovery behavior for invalid or partial state is documented.
---

Phase II requires crash-safe state.

Harden `.jri/state.json` persistence so local crashes or abrupt termination do not leave the loop unable to understand prior execution state.
