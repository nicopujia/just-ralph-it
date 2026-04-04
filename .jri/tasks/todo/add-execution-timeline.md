---
{
  "title": "Add execution timeline artifacts",
  "priority": 2,
  "assignee": "Ralph",
  "depends_on": [
    "implement-crash-safe-state-storage",
    "adopt-phase-ii-outcome-model",
    "define-stale-run-recovery-for-jri-start",
    "add-execution-timeline--needs-human"
  ],
  "acceptance_criteria": [
    "The system persists an execution timeline that records key per-iteration events.",
    "The timeline is inspectable from the CLI or stable on-disk artifacts.",
    "Tests verify timeline entries for a representative run.",
    "Documentation explains how timeline data helps explain failures and recoveries."
  ]
}
---

Phase II observability requires an execution timeline.

Add a durable event trail that explains what the loop did, in what order, and why it stopped or escalated.
