---
title: Clarify roadmap wording for failure follow-up
priority: 1
assignee: Ralph
depends_on:
  - phase-1-2-quality-gate-foundation
acceptance_criteria:
  - The roadmap and implementation docs no longer imply that generic failures must always create new tasks.
  - The documented rule makes clear when Ralph should ask for human help and how that appears in the task system.
  - Tests and runtime behavior remain aligned with the clarified rule.
  - No prompt or code path still claims that generic failures should automatically create new tasks unless that behavior is explicitly implemented.
---

The roadmap currently overstates failure handling for Phase I.

Align the wording and implementation around the narrower rule agreed in planning: generic failures do not need to auto-create new tasks, but Ralph must be able to ask for human help in a visible, inspectable way.
