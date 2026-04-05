---
title: Deploy opencode.json template on jri init
priority: 2
assignee: Ralph
depends_on: []
acceptance_criteria:
  - "A template file `opencode.json` exists at `src/jri/core/agents/opencode.json` containing the compaction config: `{ \"compaction\": { \"auto\": true, \"prune\": true, \"reserved\": 10000 } }` with a `$schema` key pointing to `https://opencode.ai/config.json`."
  - "`JriService._write_managed_files()` copies the template to `<project-root>/opencode.json` using the same `importlib.resources` pattern as agent prompts (not a hardcoded string)."
  - "`jri init` writes `opencode.json` to the project root."
  - "`jri upgrade` overwrites `opencode.json` if the template has changed (same managed-file behavior as agent prompts)."
  - "`opencode.json` is included in the init commit (added to `_INIT_COMMIT_PATHS`)."
  - "`opencode.json` is NOT added to `.gitignore` — it should be tracked in version control so the whole team shares the same compaction settings."
  - "Existing tests for `jri init` and `jri upgrade` are updated to verify `opencode.json` is created/updated."
  - "`make check` passes."
---

JRI should deploy an `opencode.json` config file to every client project so that OpenCode's auto-compaction runs with explicit, opinionated settings instead of relying on invisible defaults.

The template must live at `src/jri/core/agents/opencode.json` (alongside the agent prompt templates), loaded via `importlib.resources` — never hardcoded in Python.

Implementation notes:
- Add a `_MANAGED_CONFIG_FILENAMES` tuple (or similar) to `service.py` covering `"opencode.json"`.
- Extend `_write_managed_files()` to load the template and write it to the project root.
- Add `"opencode.json"` to `_INIT_COMMIT_PATHS` so it's included in the init commit.
- Add `"opencode.json"` to `_UPGRADE_COMMIT_PATHS` so `jri upgrade` detects changes.
- Do NOT add `opencode.json` to `.gitignore` — it must be committed to the client repo so all contributors get the same OpenCode settings.
- Follow the existing pattern for managed files (agent prompts) as closely as possible.
