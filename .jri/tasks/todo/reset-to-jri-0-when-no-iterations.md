---
title: Allow jri reset when no successful iterations
priority: 2
assignee: Ralph
depends_on: []
acceptance_criteria:
  - "When `iteration_number == 0` and tag `jri/0` exists, `jri reset` resets the default branch to `jri/0`, deletes all `ralph/*` branches, and clears `process` and `active_attempt` from state — same behavior as the existing reset for `iteration_number >= 1`."
  - "When `iteration_number == 0` and tag `jri/0` does NOT exist, `jri reset` raises `JriError` with a message indicating no iteration tag was found and suggesting to run `jri start` first."
  - "The existing test `test_reset_refuses_when_no_successful_iteration` is updated: it must now pass when `jri/0` exists (succeeds instead of raising), and a new test covers the case where `jri/0` is absent."
  - "A new test verifies that reset-to-jri/0 actually restores the working tree to the pre-Ralph state (files added by a failed/partial first iteration are gone)."
  - "CLI help text for `jri reset` is updated to reflect that it also covers the no-successful-iteration case."
  - "`make check` passes."
---

Currently `JriService.reset()` raises `JriError("no successful iteration exists yet")` when `iteration_number < 1`. But the `jri/0` tag already marks the state right before Ralphing — it's created by `_ensure_initial_iteration_tag()` at the start of `_run_loop()`.

Change the guard so that when `iteration_number == 0` and `jri/0` exists, reset targets `jri/0` instead of refusing. Only error out when `jri/0` doesn't exist (meaning `jri start` was never called).

Implementation notes:
- The only code change is in `JriService.reset()` in `src/jri/core/service.py`, lines 288-316.
- Replace the `iteration_number < 1` guard with logic that:
  1. If `iteration_number >= 1`, use tag `jri/{iteration_number}` (unchanged).
  2. If `iteration_number == 0` and `jri/0` tag exists, use tag `jri/0`.
  3. If `iteration_number == 0` and `jri/0` tag does NOT exist, raise `JriError`.
- The rest of the reset flow (checkout default branch, delete `ralph/*` branches, save state without `process`/`active_attempt`) stays the same regardless of which tag is used.
- The CLI help text in `src/jri/cli/main.py` should mention that reset works with or without successful iterations.
