---
title: Mirror schema check in git hooks
priority: 2
assignee: Ralph
depends_on:
  - phase-1-2-quality-gate-foundation
acceptance_criteria:
  - Git hook automation runs the repo schema validation gate before merge-ready commits.
  - Contributor documentation reflects whether hook coverage matches `make check` exactly or intentionally differs.
---

`make check` is now the canonical validation command, but the current git hooks still skip schema validation.
Close that gap so hook-based guardrails align with the quality gate contributors are expected to trust.
