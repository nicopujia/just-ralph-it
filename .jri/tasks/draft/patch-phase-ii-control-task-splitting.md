---
title: Patch Phase II control task boundaries
priority: 0
assignee: Human
depends_on: []
acceptance_criteria:
  - The promoted control tasks are reviewed for single-concern scope.
  - A decision is recorded for whether `add-start-recovery-and-stale-run-recovery` and `harden-stop-halt-limits-and-loop-boundaries` should be split or merely retitled.
  - Any accepted follow-up correction work is captured append-only rather than by silently rewriting promoted tasks.
---

The final audit found that two promoted control tasks are probably too broad and one title violates the intended “single concern” rule:

- `add-start-recovery-and-stale-run-recovery`
- `harden-stop-halt-limits-and-loop-boundaries`

This patch task exists to capture the cleanup decision explicitly, since promoted tasks should not be modified in place.
