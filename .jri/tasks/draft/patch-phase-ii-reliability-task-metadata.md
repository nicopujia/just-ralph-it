---
title: Patch Phase II reliability task metadata
priority: 0
assignee: Human
depends_on: []
acceptance_criteria:
  - The metadata issues in the promoted Phase II reliability tasks are reviewed before execution begins.
  - A follow-up decision records how to correct weak dependencies and priorities without silently rewriting history.
  - The affected tasks are explicitly listed: `protect-task-state-during-recovery`, `make-task-execution-idempotent`, and `eliminate-silent-failure-paths`.
---

I promoted the Phase I/II backlog before doing the final metadata audit thoroughly enough.

Because promoted tasks should not be edited in place, this patch task exists to record the reliability-metadata corrections that are still needed:

- `protect-task-state-during-recovery` likely needs a dependency on `add-start-recovery-and-stale-run-recovery` and a higher priority.
- `make-task-execution-idempotent` likely needs either a dependency on `adopt-phase-ii-outcome-model` or narrower acceptance criteria.
- `eliminate-silent-failure-paths` likely needs stronger dependencies or a narrower scope.

Resolve those corrections append-only before Ralph starts executing the affected tasks.
