---
title: Prove JRI can run on itself
priority: 1
assignee: Ralph
depends_on:
  - phase-1-2-quality-gate-foundation
  - enforce-task-testability
  - enforce-append-only-promoted-tasks
acceptance_criteria:
  - The repository contains a reproducible procedure or automated test fixture showing JRI operating against this repo shape.
  - The proof demonstrates idea-to-task-to-loop execution rather than isolated unit behavior only.
  - The proof records enough evidence to show convergence without manual rewriting of tasks.
  - Documentation points maintainers to the proof artifact or command.
---

Phase I is not complete until the repo proves its own core thesis.

Add a reproducible self-hosting proof that demonstrates JRI can manage work on a repo structured like this one.
The proof may be an end-to-end test, a deterministic fixture, or another automated artifact, but it must be runnable and inspectable inside the repo.
