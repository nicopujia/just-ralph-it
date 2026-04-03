---
title: Harden control limits and loop boundaries
priority: 2
assignee: Ralph
depends_on:
  - add-reliable-resume-and-stale-run-recovery
acceptance_criteria:
  - `stop`, `halt`, and loop-boundary behavior are documented with concrete edge-case semantics.
  - The CLI supports the agreed Phase II iteration and time-bound controls.
  - Tests cover normal stop, hard halt, iteration limit, and time-limit behavior.
  - The loop cannot continue indefinitely without making that reason visible.
---

Phase II control requires reliable stop and halt behavior, explicit boundaries, and maximum iteration or time limits.

Use this task to close the remaining control gaps after resume semantics are in place.
