---
title: Turn failures into explicit follow-up tasks
priority: 1
assignee: Ralph
depends_on:
  - phase-1-2-quality-gate-foundation
acceptance_criteria:
  - At least one supported failure path creates or preserves explicit task artifacts instead of only emitting logs.
  - The resulting follow-up work is inspectable from the task system without reading raw agent transcripts.
  - Tests cover the failure-to-follow-up behavior.
  - Documentation explains which failure classes generate new tasks and which only change task status.
---

Phase I requires failures to produce new tasks rather than confusion.

Make that behavior a repository-level guarantee instead of leaving it only to agent prompt compliance.
If different failure classes need different handling, record those rules explicitly.
