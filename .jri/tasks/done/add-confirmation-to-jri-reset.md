---
title: Add confirmation prompt to jri reset
priority: 2
assignee: Ralph
depends_on: []
acceptance_criteria:
  - "`jri reset` prompts the user with a confirmation message describing what will be discarded (e.g. target tag, uncommitted changes, ralph branches) and waits for `y/N` input."
  - "Answering `N` (or anything other than `y`/`Y`) aborts the reset with exit code 1 and a clear message."
  - "`jri reset --force` skips the confirmation prompt and proceeds directly (same behavior as the current `jri reset`)."
  - "`jri reset --help` documents the `--force` flag."
  - "Existing tests that call `JriService.reset()` directly (bypassing the CLI) still pass without modification — the confirmation is a CLI-layer concern, not a service-layer concern."
  - "New tests verify: (a) reset is aborted on negative confirmation, (b) `--force` skips confirmation, (c) the prompt message includes the target tag."
  - "`make check` passes."
---

`jri reset` is destructive — it hard-resets the default branch, deletes all `ralph/*` branches, and discards uncommitted changes. Currently it runs without any confirmation.

Add a confirmation prompt at the CLI layer (in `src/jri/cli/main.py`), before calling `service.reset()`.

Implementation notes:
- Add a `--force` / `-f` flag to the `reset` subparser in `main.py`.
- When `--force` is not set, read stdin for confirmation. Use a simple `input()` call — no new dependencies.
- The prompt should describe: target tag name, whether there are uncommitted changes, and how many `ralph/*` branches will be deleted.
- The confirmation logic lives in the CLI dispatch, not in `JriService.reset()` — the service method stays as-is so existing tests don't need to mock stdin.
- Exit code 1 on abort (user says no), exit code 0 on success.
