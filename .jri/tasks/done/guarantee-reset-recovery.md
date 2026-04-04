---
title: Guarantee jri reset recovery
priority: 1
assignee: Ralph
depends_on:
  - implement-crash-safe-state-storage
  - define-stale-run-recovery-for-jri-start
  - protect-task-state-during-recovery
acceptance_criteria:
  - `jri reset` reliably restores the last good state after representative interrupted and failed runs.
  - The reset contract is documented, including what state is restored and what local changes are discarded.
  - Tests cover reset after a successful iteration, a failed iteration, and a stale in-progress state.
---

Phase II success requires that operators can always recover with `jri reset`.

Make reset behavior explicit, reliable, and test-backed so recovery does not depend on undocumented git assumptions.
