---
title: Harden per-task execution logs
priority: 2
assignee: Ralph
depends_on:
  - define-stale-run-recovery-for-jri-start
  - adopt-phase-ii-outcome-model
acceptance_criteria:
  - Per-task logs capture normal execution, failure, and escalation paths in a durable location.
  - Known best-effort or stderr-only failure paths no longer bypass task-level logging.
  - Tests cover at least one successful task run and one failed or needs-human path with persisted logs.
  - Documentation explains where operators should look for per-task logs.
---

Phase II observability explicitly requires per-task logs.

Harden the existing logging surface so it is complete enough to explain what happened during each task run, including failure and escalation cases.
