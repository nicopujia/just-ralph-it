---
{
  "title": "Apply failed retry escalation policy",
  "priority": 1,
  "assignee": "Ralph",
  "depends_on": [
    "adopt-phase-ii-outcome-model",
    "define-stale-run-recovery-for-jri-start",
    "make-task-execution-idempotent",
    "define-needs-human-representation"
  ],
  "acceptance_criteria": [
    "Failed work is retried automatically up to 3 times before becoming `needs human`.",
    "Retry-attempt data is persisted in inspectable state or task artifacts.",
    "Tests cover first failure, retry behavior, and escalation after the third failed attempt.",
    "Documentation explains when a failure stays retryable and when it becomes `needs human`."
  ]
}
---

Phase II requires failed work to remain visible and bounded.

Implement the agreed retry policy so failed work does not loop forever and escalates to human help after three failed attempts.
