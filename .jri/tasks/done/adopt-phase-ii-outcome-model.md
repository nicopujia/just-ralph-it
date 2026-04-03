---
title: Adopt Phase II outcome vocabulary
priority: 1
assignee: Ralph
depends_on:
  - phase-1-2-quality-gate-foundation
acceptance_criteria:
  - Runtime outcomes align with the roadmap: `completed`, `failed`, and `needs human`.
  - Silent or ambiguous fallback outcomes are eliminated or made impossible to miss.
  - The agent/result protocol, persistence, and tests all use the same outcome vocabulary.
  - Documentation explains what each outcome means.
---

Phase II defines the canonical failure semantics for Ralph.

Establish the canonical outcome vocabulary and adopt it consistently across agent output parsing, persistence, tests, and documentation.
