---
title: Capture per-task diff artifacts
priority: 2
assignee: Ralph
depends_on:
  - phase-1-2-quality-gate-foundation
acceptance_criteria:
  - Each task iteration stores an inspectable diff artifact.
  - The diff artifact location and retention policy are documented.
  - Tests cover generation of the diff artifact for at least one successful iteration.
---

Phase II observability requires diffs.

Persist per-task change summaries in a stable location so users can inspect what happened in a loop iteration without reconstructing it manually.
