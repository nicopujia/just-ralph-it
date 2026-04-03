---
title: Define needs-human task representation
priority: 1
assignee: Ralph
depends_on:
  - adopt-phase-ii-outcome-model
acceptance_criteria:
  - The codebase and docs consistently define `needs human` as both a Ralph runtime outcome and a generated `Human` task in the backlog.
  - When Ralph resolves to `needs human`, the original Ralph task returns to `todo` and is blocked via `depends_on` on the generated Human task.
  - The generated Human task includes the required context and dependency link to unblock later work.
  - Tests cover the original-task transition, the generated Human task, and the resulting dependency relationship.
---

Phase II needs a single durable representation for human escalation.

Settle how a `needs human` outcome is persisted and surfaced so retry, status, and recovery work build on one model instead of inventing separate ones.
