---
title: Remove compaction text from Interrogator prompt
priority: 2
assignee: Ralph
depends_on:
  - deploy-opencode-json-template
acceptance_criteria:
  - "The Interrogator agent prompt at `src/jri/core/agents/interrogator.md` has all compaction-related text removed (lines 27-29 in the current version)."
  - "The bullet that currently reads 'Keep your active context lean: persist durable decisions to the repo, use OpenCode compaction earlier than its default behavior...' is replaced with a version that removes the compaction claim but preserves the externalization guidance: 'Keep your active context lean: persist durable decisions to the repo and never rely on long chat history when the repo can carry the same information.'"
  - "The sub-bullets about 'Durable decisions must be externalized first' and 'This policy supports indefinite long-running conversations' are removed — they are now redundant since compaction is handled automatically by OpenCode config."
  - "The deployed copy at `.opencode/agents/interrogator.md` (in this repo) is also updated to match."
  - "The learnings file `.jri/learnings.md` entry about early compaction guidance is updated to reflect that compaction is now handled via `opencode.json` config, not agent instructions."
  - "`make check` passes."
---

The Interrogator prompt currently instructs the agent to "use OpenCode compaction earlier than its default behavior," but the agent has no tool to trigger compaction — it's an infrastructure-level operation that fires automatically when the context window fills.

With the `opencode.json` config deployed (via the `deploy-opencode-json-template` task), compaction is configured at the project level with `auto: true`, `prune: true`, and a `reserved` token buffer. The agent prompt should not claim it can control compaction.

Implementation notes:
- Edit `src/jri/core/agents/interrogator.md` — this is the source template bundled with JRI.
- Also edit `.opencode/agents/interrogator.md` in this repo — this is the deployed copy used by this project's own Interrogator agent.
- Preserve the valuable guidance about externalizing decisions to repo artifacts, but strip all references to "compaction" and "triggering" it.
- Update `.jri/learnings.md` to reflect the new approach.
