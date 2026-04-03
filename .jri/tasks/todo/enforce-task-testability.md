---
title: Enforce testable task metadata
priority: 1
assignee: Ralph
depends_on:
  - phase-1-2-quality-gate-foundation
acceptance_criteria:
  - Promoted implementation tasks cannot omit acceptance criteria.
  - Validation fails with a clear error when a promoted task has empty or missing acceptance criteria.
  - Existing tests cover valid and invalid promoted task metadata.
  - Documentation explains the rule for promoted tasks without requiring acceptance criteria on every draft task.
---

Phase I requires tasks to be testable.

Implement the minimum hard enforcement needed for that requirement without making early draft capture cumbersome.
Use the hybrid policy agreed in planning: keep draft capture flexible, but require concrete acceptance criteria before a task can enter execution-ready states.
