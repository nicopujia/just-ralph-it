---
title: Add structured status output
priority: 2
assignee: Ralph
depends_on:
  - adopt-phase-ii-outcome-model
  - apply-failed-retry-and-escalation-policy
acceptance_criteria:
  - A structured output mode exists for status inspection.
  - The structured schema includes counts by state, needs-human work, retry/escalation metadata, and current run metadata.
  - Tests verify schema stability for representative states.
  - Documentation describes the intended consumers of the structured output.
---

Phase II requires structured output.

Add a machine-readable status surface that reflects the hardened runtime model and can support later UI or automation work without scraping plain text.
