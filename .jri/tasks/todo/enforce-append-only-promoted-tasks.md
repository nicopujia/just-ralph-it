---
title: Prevent in-place edits to promoted tasks
priority: 1
assignee: Ralph
depends_on:
  - phase-1-2-quality-gate-foundation
acceptance_criteria:
  - The system rejects direct mutation of promoted task files in `todo/`, `doing/`, or `done/`.
  - The supported correction path is documented and uses additive follow-up work instead of silent task rewriting.
  - Tests cover at least one allowed additive correction flow and one rejected in-place mutation flow.
---

Phase I requires append-only corrections.

Add the smallest enforceable mechanism that protects promoted tasks from being silently rewritten after promotion.
Draft tasks may remain editable while they are still under interrogation.
