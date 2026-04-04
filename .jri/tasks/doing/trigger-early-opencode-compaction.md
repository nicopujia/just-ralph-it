---
title: Trigger early Interrogator compaction
priority: 2
assignee: Ralph
depends_on:
  - add-promotion-readiness-review
acceptance_criteria:
  - Interrogator-facing docs or prompts instruct use of OpenCode's compaction tool earlier than its default behavior.
  - The policy explains that durable decisions must be externalized to repo artifacts before compaction.
  - The compaction guidance is compatible with indefinite long-running user conversations.
---

Interrogator should keep a lean context window so the same conversation can continue indefinitely.

Use OpenCode's existing compaction capability proactively, but only after durable decisions have been written into tasks, docs, or other repo artifacts Ralph can rely on later.
