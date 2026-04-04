---
title: Reject cyclic task promotions
priority: 1
assignee: Ralph
depends_on:
  - require-user-confirmation-for-promotion
acceptance_criteria:
  - Draft-to-todo promotion fails if the resulting promoted dependency graph contains a cycle.
  - The rejection identifies the cycle clearly enough for Interrogator or the user to fix it.
  - Tests cover at least one acyclic promotion case and one rejected cyclic promotion case.
  - Interrogator-facing docs or prompts describe cycle rejection as a hard promotion rule.
---

Promotion-ready tasks must not introduce dependency cycles.

Add programmatic cycle detection to the promotion boundary so graph sanity is enforced by workflow, not only by review judgment.
