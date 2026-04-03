---
title: Harden control limits and loop boundaries
priority: 2
assignee: Ralph
depends_on:
  - add-start-recovery-and-stale-run-recovery
acceptance_criteria:
  - `stop`, `halt`, and loop-boundary behavior are documented with concrete edge-case semantics.
  - The CLI supports the agreed Phase II per-task timeout control.
  - Tests cover normal stop, hard halt, iteration limit, and per-task timeout behavior.
  - The loop cannot continue indefinitely without making that reason visible.
---

Phase II control requires reliable stop and halt behavior, reliable `jri start` recovery behavior, explicit boundaries, and maximum iteration or per-task time limits.

Use this task to close the remaining control gaps after resume semantics are in place.
