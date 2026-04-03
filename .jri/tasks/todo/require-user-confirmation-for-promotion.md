---
title: Require user confirmation for draft promotion
priority: 1
assignee: Ralph
depends_on:
  - enforce-append-only-promoted-tasks
acceptance_criteria:
  - Draft-to-todo promotion requires explicit user confirmation.
  - Interrogator-facing docs or prompts make that confirmation boundary unambiguous.
  - Tests or workflow checks cover at least one rejected unconfirmed promotion and one allowed confirmed promotion.
---

Once promoted tasks become append-only, draft-to-todo promotion becomes a human approval boundary.

Implement the smallest workflow and prompt changes needed to prevent autonomous promotion without explicit user confirmation.
