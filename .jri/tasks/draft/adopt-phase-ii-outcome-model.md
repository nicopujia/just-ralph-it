---
title: Adopt Phase II execution outcomes
priority: 1
assignee: Ralph
depends_on:
  - phase-1-2-quality-gate-foundation
acceptance_criteria:
  - Runtime outcomes align with the roadmap: `completed`, `blocked`, `needs clarification`, and `needs human`.
  - Silent or ambiguous fallback outcomes are eliminated or made impossible to miss.
  - The agent/result protocol, persistence, and tests all use the same outcome model.
  - Documentation explains what each outcome means and how the loop reacts.
---

Phase II defines the canonical failure semantics for Ralph.

Update the implementation so roadmap outcomes are first-class and consistently represented across agent output parsing, task movement, state, status, and recovery behavior.
