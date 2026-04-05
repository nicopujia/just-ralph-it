---
title: Fix Interrogator prompt inaccuracies
priority: 2
assignee: Ralph
depends_on: []
acceptance_criteria:
  - "The constraint `offer at most 5 concrete options plus \`Other\`` is changed to `offer at most 5 concrete options` — OpenCode already shows a 'type your own answer' field, so `Other` is redundant."
  - "The constraint `DO NOT wait for user confirmation to commit` is updated to clarify scope: the Interrogator should commit draft task files and README.md changes, but must NOT manually commit around `jri promote` — the CLI already handles promotion commits."
  - "The deployed copy at `.opencode/agents/interrogator.md` (in this repo) is also updated to match."
  - "`make check` passes."
---

Two inaccuracies in the Interrogator prompt need fixing:

### 1. Remove "Other" from question options

Line 81 currently reads:
> If you ask a multiple-choice question, offer at most 5 concrete options plus `Other`; point which one you suggest and why.

OpenCode's `question` tool already appends a "Type your own answer" field by default when `custom` is enabled. Including `Other` as an explicit option is redundant and wastes one of the 5 slots.

Change to:
> If you ask a multiple-choice question, offer at most 5 concrete options; point which one you suggest and why.

### 2. Clarify commit scope — don't commit around `jri promote`

Line 113 currently reads:
> DO NOT wait for user confirmation to commit; do it by default after meaningful persisted progress whenever you create or update tasks or `README.md` content.

This is too broad. The Interrogator tries to commit before or after `jri promote`, but `jri promote` already creates its own commit. Double-committing creates noise.

Change to:
> DO NOT wait for user confirmation to commit draft task files or `README.md` changes; do it by default after meaningful persisted progress. However, never manually commit around `jri promote` — the CLI already manages promotion commits.

Implementation notes:
- Edit `src/jri/core/agents/interrogator.md` — this is the bundled source template.
- Also edit `.opencode/agents/interrogator.md` — this is the deployed copy used by this project's own Interrogator.
- Both files must be identical after the edit.
