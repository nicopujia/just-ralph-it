---
title: Harden stop and halt semantics
priority: 2
assignee: Ralph
depends_on:
  - define-stale-run-recovery-for-jri-start
acceptance_criteria:
  - `stop` and `halt` behavior are documented with concrete edge-case semantics.
  - Tests cover graceful stop during active work and hard halt of an active run.
  - `stop`, `halt`, and `jri start` recovery behavior remain consistent after interruptions.
---

Phase II control requires reliable stop and halt behavior that composes cleanly with `jri start` recovery.

Use this task to close the control-semantics gaps after stale-run recovery semantics are in place.
