---
name: references
description: Shared references for OpenCode and The Ralph Playbook. Use when you need source context from either repo.
---

1. Check that both reference repos exist under `@.opencode/skills/references/`:
   - `opencode/` from `https://github.com/anomalyco/opencode`
   - `ralph-playbook/` from `https://github.com/ClaytonFarr/ralph-playbook`
2. If either repo is missing, fetch submodules:
   ```bash
   git submodule update --init --recursive
   ```
3. Use the repo that matches the user request, or both when the task spans OpenCode behavior and Ralph guidance.
4. Spawn a subagent to gather the relevant context from those references.
