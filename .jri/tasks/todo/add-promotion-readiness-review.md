---
title: Add promotion-readiness review workflow
priority: 1
assignee: Ralph
depends_on:
  - require-user-confirmation-for-promotion
acceptance_criteria:
  - Before every draft-to-todo promotion batch, Interrogator runs subagent-assisted promotion-readiness review.
  - The review checks both task completeness and dependency-graph sanity, including cycle detection.
  - The number of review subagents is guided by task complexity and quantity rather than being fixed.
  - Interrogator-facing docs or prompts describe this review workflow clearly.
---

Promotion should not rely only on the main agent's judgment.

Add a standard review workflow that uses subagents to pressure-test draft tasks and their dependency graph before each promotion batch.
