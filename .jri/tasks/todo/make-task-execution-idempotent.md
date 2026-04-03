---
title: Make task execution retries idempotent
priority: 2
assignee: Ralph
depends_on:
  - implement-crash-safe-state-storage
  - add-start-recovery-and-stale-run-recovery
acceptance_criteria:
  - Retrying an interrupted task does not duplicate finalization side effects.
  - The system records enough execution-attempt metadata to distinguish a retry from a first run.
  - Tests cover rerunning a partially completed task safely.
  - The idempotency contract is documented for future backend work.
---

Phase II requires idempotent task execution.

Introduce the minimum execution journal or equivalent mechanism needed to make reruns safe and explainable.
