---
title: Prove JRI can run on itself
priority: 1
assignee: Ralph
depends_on:
  - phase-1-2-quality-gate-foundation
  - enforce-task-testability
  - enforce-append-only-promoted-tasks
acceptance_criteria:
  - The repository contains an end-to-end self-hosting proof test showing JRI operating against this repo shape.
  - The proof demonstrates idea-to-task-to-loop execution rather than isolated unit behavior only.
  - The proof records enough evidence to show convergence without manual rewriting of tasks.
  - The proof is skipped by default and documented similarly to the existing opt-in live test pattern.
---

Phase I is not complete until the repo proves its own core thesis.

Add a reproducible end-to-end self-hosting proof that demonstrates JRI can manage work on a repo structured like this one.
Model the ergonomics after the existing opt-in live test pattern: present in the test suite, skipped by default, and easy for maintainers to run intentionally.
